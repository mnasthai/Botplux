from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

from wechat_receiver.ai import codex_provider as codex
from wechat_receiver.ai.profiles import DEFAULT_PROFILE, PROFILES


# A real child process exercises Windows pipes, early notifications, and timeout
# cleanup. It has no network code and cannot execute model-generated commands.
_SERVER = r'''
import json,sys,time
from pathlib import Path
scenario=json.loads(sys.argv[1])
config=json.loads(sys.argv[2])
if scenario in ('inherited_mcp','managed_mcp'):
    inherited={'global.server':{'command':'MUST_NOT_RUN','enabled':True}}
    for name,value in config['mcp_servers'].items():inherited.setdefault(name,{}).update(value)
    if scenario=='managed_mcp':inherited['global.server']['enabled']=True
    config['mcp_servers']=inherited
def send(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialized': continue
    if scenario=='check_only' and method in {'thread/start','turn/start'}:
        raise RuntimeError('Availability must not create a model turn')
    params=message.get('params',{})
    result={}
    if method=='initialize':
        result={'userAgent':'test','codexHome':str(Path.cwd()/'codex_home'),'platformFamily':'windows','platformOs':'windows'}
    elif method=='config/read':
        if scenario=='config_drift':config['features']['shell_tool']=True
        result={'config':config,'origins':{}}
    elif method=='account/read':
        assert params['refreshToken'] is False
        result={'account':None if scenario=='no_login' else {'type':'chatgpt'},'requiresOpenaiAuth':True}
    elif method=='model/list':
        result={'data':[
            {'model':'gpt-5.6-sol','supportedReasoningEfforts':[{'reasoningEffort':'high'}]},
            {'model':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]},
        ],'nextCursor':None}
    elif method=='thread/start':
        assert all(value.get('enabled',True) is False for value in config['mcp_servers'].values())
        assert params['model']==config['model']
        assert params['modelProvider']=='openai'
        assert params['dynamicTools']==[] and params['environments']==[]
        assert params['selectedCapabilityRoots']==[] and params['ephemeral'] is True
        assert params['allowProviderModelFallback'] is False
        assert params['approvalPolicy']=='never' and params['approvalsReviewer']=='user'
        assert params['sandbox']=='read-only'
        result={'model':'wrong-model' if scenario=='thread_drift' else params['model'],
            'modelProvider':'openai','reasoningEffort':config['model_reasoning_effort'],
            'approvalPolicy':'never','approvalsReviewer':'user',
            'sandbox':{'type':'readOnly','networkAccess':False},'instructionSources':[],
            'thread':{'id':'thread-1','ephemeral':True}}
        if scenario=='effort_drift':result['reasoningEffort']='low'
        if scenario=='global_instructions':result['instructionSources']=[str(Path.cwd()/'codex_home'/'AGENTS.md')]
        if scenario=='unknown_instructions':result['instructionSources']=[str(Path.cwd()/'other'/'AGENTS.md')]
    elif method=='turn/start':
        assert params['model']==config['model'] and params['effort']==config['model_reasoning_effort']
        assert params['approvalPolicy']=='never' and params['approvalsReviewer']=='user'
        assert params['sandboxPolicy']=={'type':'readOnly','networkAccess':False}
        assert params['environments']==[]
        if scenario=='hang':time.sleep(30)
        if scenario=='malformed':
            print('not-json SECRET_AUTH_RESPONSE',flush=True)
            time.sleep(30)
        if scenario=='rpc_error':
            send({'id':message['id'],'error':{'code':401,'message':'SECRET_AUTH_RESPONSE'}})
            continue
        if scenario=='retryable_error':
            send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1',
                'willRetry':True,'error':{'message':'SECRET_AUTH_RESPONSE',
                    'codexErrorInfo':'serverOverloaded'}}})
        if scenario=='fatal_error':
            send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1',
                'willRetry':False,'error':{'message':'SECRET_AUTH_RESPONSE',
                    'codexErrorInfo':{'responseStreamDisconnected':{'httpStatusCode':503}}}}})
            continue
        if scenario=='tool_request':
            send({'id':'server-1','method':'item/commandExecution/requestApproval','params':{'command':'forbidden'}})
            continue
        if scenario=='tool_item':
            send({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'tool','type':'commandExecution'}}})
            continue
        commentary={'id':'comment','type':'agentMessage','phase':'commentary','text':'not the answer'}
        answer={'id':'answer','type':'agentMessage','phase':'final_answer','text':'你好，世界🙂'}
        if scenario=='empty':answer['text']=' '
        # Notifications can race ahead of the turn/start response.
        for item in (commentary,answer):
            send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':item}})
        turn={'id':'turn-1','status':'failed' if scenario=='failed' else 'completed',
            'error':{'message':'SECRET_AUTH_RESPONSE'} if scenario=='failed' else None,'items':[commentary,answer]}
        send({'method':'turn/completed','params':{'threadId':'thread-1','turn':turn}})
        result={'turn':{'id':'turn-1','status':'inProgress','items':[],'error':None}}
    send({'id':message['id'],'result':result})
'''


class CodexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temporary.name)
        self.real_popen = subprocess.Popen
        self.children: list[subprocess.Popen] = []
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.scenario = "ok"
        self.version = "0.145.0"
        self.catalog_tool_mode = "code_mode_only"

        def run(command, **kwargs):
            if command[-1] == "--version":
                data = f"codex-cli {self.version}\n".encode()
            else:
                self.assertEqual(["debug", "models", "--bundled"], command[-3:])
                data = json.dumps({"models": [{"slug": profile.model,
                    "tool_mode": self.catalog_tool_mode, "experimental_supported_tools": []}
                    for profile in PROFILES.values()]}).encode()
            return subprocess.CompletedProcess(command, 0, stdout=data)

        def popen(command, **kwargs):
            self.commands.append(command)
            self.environments.append(kwargs["env"])
            self.assertEqual(self.cwd, kwargs["cwd"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
            self.assertNotIn("shell", kwargs)
            config = {}
            for index, value in enumerate(command):
                if value == "-c":
                    config.update(tomllib.loads(command[index + 1]))
            child = self.real_popen([sys.executable, "-u", "-X", "utf8", "-c", _SERVER,
                                    json.dumps(self.scenario), json.dumps(config)], **kwargs)
            self.children.append(child)
            return child

        self.run_patch = patch.object(codex.subprocess, "run", side_effect=run)
        self.popen_patch = patch.object(codex.subprocess, "Popen", side_effect=popen)
        self.run_patch.start()
        self.popen_patch.start()

    def tearDown(self) -> None:
        self.run_patch.stop()
        self.popen_patch.stop()
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
        self.temporary.cleanup()

    def provider(self, timeout: float = 5, *, profile: str = DEFAULT_PROFILE) -> codex.CodexProvider:
        return codex.CodexProvider(sys.executable, self.cwd, timeout, profile=profile)

    def test_completed_items_return_only_final_text_without_duplicates(self) -> None:
        answer = self.provider().complete('问题带换行\n"引号" 和中文', instructions="限定上下文")
        self.assertEqual("你好，世界🙂", answer)
        self.assertEqual(1, len(self.children))
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual([], list(self.cwd.iterdir()))

    def test_admin_flag_keeps_tools_disabled_and_secrets_out_of_environment(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CUSTOM_MCP_TOKEN": "secret",
                                      "CODEX_THREAD_ID": "parent", "CODEX_HOME": "existing-home"}):
            self.assertEqual("你好，世界🙂", self.provider().complete("你好", admin_tools=True))
        env = self.environments[0]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CUSTOM_MCP_TOKEN", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertEqual("existing-home", env["CODEX_HOME"])
        command = self.commands[0]
        self.assertIn("--strict-config", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn('web_search="disabled"', command)

    def test_availability_does_not_start_a_thread_or_model_turn(self) -> None:
        self.scenario = "check_only"
        result = self.provider().check_available()
        self.assertTrue(result["available"])
        self.assertEqual("gpt-5.6-luna", result["model"])
        self.assertEqual("medium", result["reasoning_effort"])
        self.assertFalse(result["tools_enabled"])

    def test_only_approved_profiles_can_be_selected_at_task_boundaries(self) -> None:
        self.assertEqual("luna", DEFAULT_PROFILE)
        self.assertEqual(
            {"sol": ("gpt-5.6-sol", "high"), "luna": ("gpt-5.6-luna", "medium")},
            {name: (profile.model, profile.reasoning_effort) for name, profile in PROFILES.items()},
        )
        with self.assertRaises(FrozenInstanceError):
            PROFILES["sol"].model = "unapproved"

        provider = self.provider()
        self.assertEqual("luna", provider.profile)
        self.assertEqual("gpt-5.6-luna", provider.model)
        self.assertEqual("medium", provider.reasoning_effort)
        self.assertEqual("你好，世界🙂", provider.complete("luna"))

        provider.select_profile("sol")
        self.assertEqual("sol", provider.profile)
        self.assertEqual("gpt-5.6-sol", provider.model)
        self.assertEqual("high", provider.reasoning_effort)
        self.assertEqual("你好，世界🙂", provider.complete("sol"))
        result = provider.check_available()
        self.assertEqual("gpt-5.6-sol", result["model"])
        self.assertEqual("high", result["reasoning_effort"])

        provider.select_profile("luna")
        for invalid in ("gpt-5.6-sol", "", "SOL", None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(codex.CodexError, "档位无效"):
                    provider.select_profile(invalid)
                self.assertEqual("luna", provider.profile)

    def test_profiles_reject_a_returned_wrong_model_or_effort(self) -> None:
        for profile in PROFILES:
            for scenario in ('thread_drift', 'effort_drift'):
                with self.subTest(profile=profile, scenario=scenario):
                    self.scenario = scenario
                    with self.assertRaisesRegex(codex.CodexError, "隔离"):
                        self.provider(profile=profile).complete("test")

    def test_missing_login_and_configuration_drift_fail_closed(self) -> None:
        for scenario, expected in (("no_login", "登录"), ("config_drift", "关闭"), ("thread_drift", "隔离")):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                with self.assertRaisesRegex(codex.CodexError, expected):
                    self.provider().complete("test")
                self.assertIsNotNone(self.children[-1].poll())

    def test_version_or_catalog_changes_are_rejected_before_server_start(self) -> None:
        self.version = "0.146.0"
        with self.assertRaisesRegex(codex.CodexError, "版本"):
            self.provider().complete("test")
        self.assertEqual([], self.children)
        self.version = "0.145.0"
        self.catalog_tool_mode = "direct"
        with self.assertRaisesRegex(codex.CodexError, "工具配置"):
            self.provider().complete("test")
        self.assertEqual([], self.children)

    def test_global_mcp_deep_merge_is_disabled_before_a_thread_can_start(self) -> None:
        self.scenario = "inherited_mcp"
        provider = self.provider()
        self.assertEqual("你好，世界🙂", provider.complete("test"))
        self.assertEqual(2, len(self.children))
        self.assertEqual("你好，世界🙂", provider.complete("test again"))
        self.assertEqual(3, len(self.children))
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_managed_mcp_that_cannot_be_disabled_fails_closed(self) -> None:
        self.scenario = "managed_mcp"
        with self.assertRaisesRegex(codex.CodexError, "MCP"):
            self.provider().complete("test")
        self.assertEqual(2, len(self.children))

    def test_only_known_global_instructions_are_allowed(self) -> None:
        self.scenario = "global_instructions"
        self.assertEqual("你好，世界🙂", self.provider().complete("test"))
        self.scenario = "unknown_instructions"
        with self.assertRaisesRegex(codex.CodexError, "隔离"):
            self.provider().complete("test")

    def test_server_errors_never_expose_raw_auth_responses_or_retry(self) -> None:
        for scenario in ("rpc_error", "failed", "malformed", "tool_request", "tool_item", "empty"):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                before = len(self.children)
                with self.assertRaises(codex.CodexError) as caught:
                    self.provider().complete("test")
                self.assertNotIn("SECRET_AUTH_RESPONSE", str(caught.exception))
                self.assertEqual(before + 1, len(self.children))
                self.assertIsNotNone(self.children[-1].poll())

    def test_retryable_server_error_allows_same_turn_to_complete(self) -> None:
        self.scenario = "retryable_error"
        self.assertEqual("你好，世界🙂", self.provider().complete("test"))

    def test_fatal_server_error_exposes_only_allowlisted_code(self) -> None:
        self.scenario = "fatal_error"
        with self.assertRaises(codex.CodexError) as caught:
            self.provider().complete("test")
        self.assertEqual("codex_response_stream_disconnected", caught.exception.code)
        self.assertNotIn("SECRET_AUTH_RESPONSE", str(caught.exception))

    def test_timeout_kills_only_the_owned_child_and_cleans_runtime(self) -> None:
        self.scenario = "hang"
        began = time.monotonic()
        with self.assertRaisesRegex(codex.CodexError, "超时"):
            self.provider(timeout=0.5).complete("test")
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual(1, len(self.children))
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual([], list(self.cwd.iterdir()))

    def test_all_approval_categories_are_denied(self) -> None:
        cases = {
            "item/commandExecution/requestApproval": {"decision": "cancel"},
            "item/fileChange/requestApproval": {"decision": "cancel"},
            "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
            "mcpServer/elicitation/request": {"action": "decline"},
            "item/tool/call": {"success": False, "contentItems": []},
        }
        for method, expected in cases.items():
            with self.subTest(method=method):
                rpc = codex._Rpc.__new__(codex._Rpc)
                sent = []
                rpc.send = sent.append
                with self.assertRaises(codex.CodexError):
                    rpc._deny({"id": "r", "method": method})
                self.assertEqual([{"id": "r", "result": expected}], sent)

    def test_invalid_input_does_not_start_a_process(self) -> None:
        for prompt in ("", "   ", None, "x" * (codex._MAX_PROMPT_BYTES + 1), "\ud800"):
            with self.subTest(prompt_type=type(prompt).__name__):
                with self.assertRaises(codex.CodexError):
                    self.provider().complete(prompt)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(codex.CodexError):
                self.provider(timeout)
        self.assertEqual([], self.children)


if __name__ == "__main__":
    unittest.main()
