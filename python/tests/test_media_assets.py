import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from wechat_receiver.media_assets import decode_media_asset, find_media_asset, _decoded_image_asset
from wechat_receiver.normalize import normalize
from wechat_receiver.store import Store


class MediaAssetTests(unittest.TestCase):
    def test_encoded_image_converts_once_and_keeps_original_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = b'wxgf synthetic container'
            digest = hashlib.sha256(original).hexdigest()
            name = digest + '.wxgf'
            (root / name).write_bytes(original)
            record = dict(kind='media_encoded_asset', schema_version=2, msg_type=3,
                          status='encoded', asset_name=name, sha256=digest,
                          byte_length=len(original), image_variant='unknown', resource_kind=2)
            decoded = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR'
                       b'\x00\x00\x00\x02\x00\x00\x00\x02synthetic decoded image')
            decoder = Mock(return_value=decoded)
            module = SimpleNamespace(decode_wxgf=decoder)
            with patch.dict('sys.modules', {'wechat_receiver.image_codec': module}):
                first = _decoded_image_asset(record, root)
                second = _decoded_image_asset(record, root)
                self.assertEqual('available', first.status)
                self.assertEqual(first, second)
                self.assertEqual(decoded, second.read_bytes())
                decoder.assert_called_once_with(original)
                self.assertEqual(original, (root / name).read_bytes())
                (root / 'decoded' / (digest + '.json')).unlink()
                decoder.side_effect = ValueError('unsupported animation')
                failed = _decoded_image_asset(record, root)
                self.assertEqual('encoded', failed.status)
                self.assertEqual(original, failed.read_bytes())

    def test_late_thumbnail_does_not_replace_standard_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / 'messages.sqlite3')
            try:
                message = normalize(dict(kind='item', seq=1, msg_type=3, content='<msg/>',
                                         raw_fields={'12': '123456789'}),
                                    session_id='current', self_id='self')
                payload = b'\xff\xd8\xfffixture\xff\xd9'
                digest = hashlib.sha256(payload).hexdigest()
                name = digest + '.jpg'
                (root / name).write_bytes(payload)
                source = store.new_source(root / 'observer.jsonl', 'fixture')
                for offset, variant, resource_kind in ((1, 'standard', 2), (2, 'thumbnail', 1)):
                    record = dict(kind='media_asset', schema_version=2, session_id='current',
                                  msg_type=3, message_id='123456789', status='available',
                                  sha256=digest, asset_name=name, byte_length=len(payload),
                                  image_variant=variant, resource_kind=resource_kind)
                    store.db.execute('''INSERT INTO raw_events
                        (source_id,start_offset,end_offset,raw,parse_status,session_id,kind,canonical_json)
                        VALUES (?,?,?,?,?,?,?,?)''',
                        (source['id'], offset, offset+1, b'{}', 'ok', 'current', 'media_asset', json.dumps(record)))
                asset = find_media_asset(store.db, message, root)
                self.assertEqual('available', asset.status)
                self.assertEqual('standard', asset.image_variant)
                self.assertEqual(2, asset.resource_kind)
                self.assertEqual(payload, asset.read_bytes())
                # A larger unsupported container must not hide a valid image.
                encoded = b'wxgf unsupported test container'
                encoded_hash = hashlib.sha256(encoded).hexdigest()
                (root / (encoded_hash + '.wxgf')).write_bytes(encoded)
                encoded_record = record | dict(kind='media_encoded_asset', status='encoded',
                    asset_name=encoded_hash + '.wxgf', sha256=encoded_hash, byte_length=len(encoded),
                    image_variant='original', resource_kind=3, width=256, height=128)
                store.db.execute('''INSERT INTO raw_events
                    (source_id,start_offset,end_offset,raw,parse_status,session_id,kind,canonical_json)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (source['id'], 3, 4, b'{}', 'ok', 'current', 'media_encoded_asset', json.dumps(encoded_record)))
                asset = find_media_asset(store.db, message, root)
                self.assertEqual('available', asset.status)
                self.assertEqual('standard', asset.image_variant)
                self.assertEqual('higher_resolution_resource_unavailable', asset.reason)
            finally:
                store.close()

    def test_only_hash_named_verified_files_can_be_opened(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b'\x02#!SILK_V3\x02\x00ab'
            digest = hashlib.sha256(payload).hexdigest()
            name = digest + '.silk'
            path = root / name
            path.write_bytes(payload)
            record = dict(kind='media_asset', schema_version=2, msg_type=34, status='available',
                          asset_name=name, byte_length=len(payload), sha256=digest)
            asset = decode_media_asset(record, root)
            self.assertEqual('available', asset.status)
            self.assertEqual(payload, asset.read_bytes())
            self.assertNotIn(str(root), repr(asset))
            for changed in ({'asset_name': '../' + name}, {'sha256': 'b' * 64}, {'byte_length': True},
                            {'msg_type': 3}, {'byte_length': len(payload) + 1}):
                self.assertEqual('unavailable', decode_media_asset(record | changed, root).status)
            path.write_bytes(b'x' * len(payload))
            with self.assertRaisesRegex(ValueError, 'changed'):
                asset.read_bytes()

    def test_session_server_id_and_type_associate_late_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / 'messages.sqlite3')
            try:
                message = normalize(dict(kind='item', seq=1, msg_type=34, content='<msg/>',
                                         content_read={'status': 'ok'}, raw_fields={'12': '123456789'}),
                                    session_id='current', self_id='self')
                self.assertEqual('pending', find_media_asset(store.db, message, root).status)
                asset_record = dict(kind='media_asset', schema_version=2, msg_type=34, status='unavailable',
                                    message_id='123456789')
                # Synthetic committed diagnostics: no native process is contacted.
                source = store.new_source(root / 'observer.jsonl', 'fixture')
                for offset, session, message_type in ((1, 'old', 34), (2, 'current', 3), (3, 'current', 34)):
                    record = asset_record | {'session_id': session, 'msg_type': message_type}
                    store.db.execute('''INSERT INTO raw_events
                        (source_id,start_offset,end_offset,raw,parse_status,session_id,kind,canonical_json)
                        VALUES (?,?,?,?,?,?,?,?)''',
                        (source['id'], offset, offset+1, b'{}', 'ok', session, 'media_asset', json.dumps(record)))
                    expected = 'unavailable' if offset == 3 else 'pending'
                    self.assertEqual(expected, find_media_asset(store.db, message, root).status)
            finally:
                store.close()
