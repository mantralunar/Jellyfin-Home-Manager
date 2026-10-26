import importlib.util
import unittest
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("jhm", ROOT / "app.py")
jhm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jhm)


class FakeJellyfin(jhm.Jellyfin):
    def __init__(self):
        self.base = "http://mock-jellyfin"
        self.users_data = {
        "u1": {"Id": "u1", "Name": "Alice", "Policy": {}, "Configuration": {"OrderedViews": ["b", "a"], "LatestItemsExcludes": [], "MyMediaExcludes": []}},
        "u2": {"Id": "u2", "Name": "Bob", "Policy": {}, "Configuration": {"OrderedViews": ["b"], "LatestItemsExcludes": [], "MyMediaExcludes": []}},
        }
        self.views_data = {"u1": ["a", "b"], "u2": ["b"]}

    def request(self, method, path, body=None):
        if method == "GET" and path == "/Users": return list(self.users_data.values())
        if method == "GET" and path == "/Library/VirtualFolders": return [{"Name": "Films", "ItemId": "a", "CollectionType": "movies"}, {"Name": "Shows", "ItemId": "b", "CollectionType": "tvshows"}]
        parts = path.split("/")
        if method == "GET" and len(parts) == 4 and parts[3] == "Views": return {"Items": [{"Id": x} for x in self.views_data[parts[2]]]}
        if method == "GET" and len(parts) == 3: return self.users_data[parts[2]]
        if method == "POST" and len(parts) == 4 and parts[3] == "Configuration":
            self.users_data[parts[2]]["Configuration"] = body
            return None
        raise AssertionError((method, path))


class Tests(unittest.TestCase):
    def test_discovery_uses_live_ids_and_names(self):
        self.assertEqual(FakeJellyfin().libraries(), [
            {"id": "a", "name": "Films", "collectionType": "movies"},
            {"id": "b", "name": "Shows", "collectionType": "tvshows"},
        ])

    def test_current_jellyfin_authorization_header(self):
        jf = FakeJellyfin()
        jf.token = "abc123"
        header = jf.authorization_header()
        self.assertTrue(header.startswith('MediaBrowser '))
        self.assertIn('Token="abc123"', header)
        self.assertNotIn("X-Emby-Token", header)

    def test_access_filter_preserves_master_order_and_toggles(self):
        master = {"libraries": [
            {"id": "a", "name": "Films", "myMedia": True, "latest": False},
            {"id": "b", "name": "Shows", "myMedia": False, "latest": True},
        ], "settings": {"SubtitleMode": "Smart"}}
        current = FakeJellyfin().users_data["u2"]
        config, rows = jhm.desired_config(current, master, {"b"})
        self.assertEqual([x["id"] for x in rows], ["b"])
        self.assertEqual(config["OrderedViews"], ["b"])
        self.assertEqual(config["MyMediaExcludes"], ["b"])
        self.assertEqual(config["LatestItemsExcludes"], [])

    def test_update_and_read_back(self):
        jf = FakeJellyfin(); config = dict(jf.user("u1")["Configuration"]); config["OrderedViews"] = ["a", "b"]
        jf.update_configuration("u1", config)
        self.assertEqual(jf.user("u1")["Configuration"]["OrderedViews"], ["a", "b"])

    def test_master_validation_rejects_duplicate_ids(self):
        with self.assertRaises(jhm.ApiError):
            jhm.validate_master({"libraries": [{"id": "a"}, {"id": "a"}], "settings": {}})

    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            jhm.atomic_json(target, {"safe": True})
            self.assertEqual(target.read_text().strip(), '{\n  "safe": true\n}')
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)


if __name__ == "__main__": unittest.main()
