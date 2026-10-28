"""The security monitor: classification, tags, exceptions, the register, the hash-chained log and the API."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import ROOT, handler, netwatch_api
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.netwatch import NetWatch, NetwatchError, ChainLog, behavior_string, classify_remote
from serve.service import Service
from serve.util.text import ByteTokenizer


class FakeSource:
    available = True

    def __init__(self):
        self.procs = [{"pid": 10, "name": "basalt.exe", "role": "engine"}, {"pid": 11, "name": "curl.exe", "role": "tool"}]
        self.conns = {}
        self.files = {}
        self.env = {}
        self.ports = []

    def processes(self):
        return self.procs

    def connections(self, pid):
        return self.conns.get(pid, [])

    def open_files(self, pid):
        return self.files.get(pid, [])

    def environ(self, pid):
        return self.env.get(pid, {})

    def listeners(self):
        return [{"ip": "127.0.0.1", "port": p, "pid": 99} for p in self.ports]


def conn(ip, port=443, proto="tcp", laddr=("192.168.1.5", 50000), status="ESTABLISHED"):
    return {"proto": proto, "laddr": laddr, "raddr": (ip, port), "status": status}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = FakeSource()
        self.now = 1000.0
        self.hosts = {}
        self.nw = NetWatch(Path(self.tmp.name), self.src, lambda ip: self.hosts.get(ip, ""), lambda: self.now)

    def poll(self):
        self.now += 1
        return self.nw.poll_once()


class TestDetection(_Base):
    def test_classify(self):
        self.assertEqual(classify_remote("127.0.0.1"), "ignore")
        self.assertEqual(classify_remote("::1"), "ignore")
        self.assertEqual(classify_remote("169.254.1.1"), "ignore")
        self.assertEqual(classify_remote("192.168.1.9"), "lan")
        self.assertEqual(classify_remote("8.8.8.8"), "public")

    def test_loopback_ignored(self):
        self.src.conns[10] = [conn("127.0.0.1", 7232), conn("::1", 80), conn("169.254.3.3")]
        self.assertEqual(self.poll(), [])

    def test_engine_public_connection_is_flagged_once(self):
        self.src.conns[10] = [conn("93.184.216.34")]
        ev = self.poll()
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["suspicious"])
        self.assertEqual(ev[0]["process"], "basalt")
        self.assertEqual(self.poll(), [])                      # the same connection is not reported twice
        self.assertEqual(len(self.nw.alerts()), 1)

    def test_engine_lan_connection_is_not_suspicious(self):
        self.src.conns[10] = [conn("192.168.1.20")]
        ev = self.poll()
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["suspicious"])
        self.assertEqual(self.nw.alerts(), [])

    def test_tool_public_connection_is_plain(self):
        self.src.conns[11] = [conn("93.184.216.34")]
        ev = self.poll()
        self.assertFalse(ev[0]["suspicious"])

    def test_alibaba_tag_on_tool(self):
        self.hosts["47.1.2.3"] = "x.dashscope.aliyuncs.com"
        self.src.conns[11] = [conn("47.1.2.3")]
        ev = self.poll()
        self.assertIn("alibaba_cn_domain", ev[0]["tags"])
        self.assertTrue(ev[0]["suspicious"])

    def test_proxy_env_and_doh_tags(self):
        self.src.env[11] = {"https_proxy": "http://x:1"}
        self.src.conns[11] = [conn("1.1.1.1", 853)]
        ev = self.poll()
        self.assertEqual(set(ev[0]["tags"]), {"proxy_env", "doh_or_tor"})

    def test_local_proxy_listener(self):
        self.src.ports = [7890]
        self.src.conns[11] = [conn("93.184.216.34")]
        self.assertIn("local_proxy_listener", self.poll()[0]["tags"])

    def test_no_agent_call_only_when_hook_used(self):
        self.src.conns[10] = [conn("93.184.216.34")]
        self.assertNotIn("no_agent_call", self.poll()[0]["tags"])
        self.nw.note_tool_call(self.now, "web_fetch", "https://example.org")
        self.src.conns[10] = [conn("93.184.216.35")]
        self.assertIn("no_agent_call", self.poll()[0]["tags"])
        self.hosts["93.184.216.36"] = "example.org"
        self.nw.note_tool_call(self.now, "web_fetch", "https://example.org/x")
        self.src.conns[10] = [conn("93.184.216.36")]
        self.assertNotIn("no_agent_call", self.poll()[0]["tags"])

    def test_pending_host_waits_then_is_released(self):
        answers = {}
        self.nw.resolver = lambda ip: answers.get(ip)
        self.src.conns[11] = [conn("47.1.2.3")]
        self.assertEqual(self.poll(), [])
        answers["47.1.2.3"] = "a.alicdn.com"
        ev = self.poll()
        self.assertIn("alibaba_cn_domain", ev[0]["tags"])

    def test_disk_outside_expected_and_archive(self):
        with tempfile.TemporaryDirectory() as outside:
            f = Path(outside) / "out.zip"
            f.write_bytes(b"x" * 10)
            self.src.files[10] = [{"path": str(f), "mode": "w"}, {"path": str(Path(self.tmp.name) / "inside.bin"), "mode": "w"}]
            (Path(self.tmp.name) / "inside.bin").write_bytes(b"1")
            self.nw.store.settings["expected_folders"] = []
            self.nw.expected_cache = [self.tmp.name]
            ev = self.poll()
            self.assertEqual(len(ev), 1)
            self.assertEqual(set(ev[0]["tags"]), {"disk_outside_expected", "archive_or_staging"})
            self.assertEqual(self.poll(), [])

    def test_check_failure_never_raises(self):
        class Broken(FakeSource):
            def processes(self):
                raise RuntimeError("boom")
        nw = NetWatch(Path(self.tmp.name) / "b", Broken(), lambda ip: "", lambda: 1.0)
        self.assertEqual(nw.poll_once(), [])


class TestExceptions(_Base):
    def flagged(self):
        self.src.conns[10] = [conn("93.184.216.34", 443)]
        self.hosts["93.184.216.34"] = "example.org"
        return self.poll()[0]

    def test_behavior_string(self):
        self.assertEqual(behavior_string("basalt", "tcp", 443, ["b", "a"]), "basalt tcp outbound port 443 tags=a,b")

    def test_suppress_by_behavior(self):
        self.nw.save_exception({"kind": "behavior", "pattern": r"^basalt tcp outbound port 443 "})
        ev = self.flagged()
        self.assertTrue(ev["suppressed"])
        self.assertEqual(self.nw.alerts(), [])
        self.assertEqual(self.nw.status()["counters"]["suppressed"], 1)
        self.assertEqual(len(self.nw.events_list()), 1)        # still logged

    def test_suppress_by_resource(self):
        self.nw.save_exception({"kind": "resource", "pattern": r"example\.org"})
        self.assertTrue(self.flagged()["suppressed"])

    def test_kinds_are_separate(self):
        self.nw.save_exception({"kind": "behavior", "pattern": r"example\.org"})      # host is not in the behaviour string
        self.assertFalse(self.flagged()["suppressed"])

    def test_disabled_exception_does_not_suppress(self):
        self.nw.save_exception({"kind": "resource", "pattern": "example", "enabled": False})
        self.assertFalse(self.flagged()["suppressed"])

    def test_invalid_regex_rejected(self):
        with self.assertRaises(NetwatchError) as cm:
            self.nw.save_exception({"kind": "behavior", "pattern": "(unclosed"})
        self.assertEqual(cm.exception.code, "bad_pattern")
        with self.assertRaises(NetwatchError):
            self.nw.save_exception({"kind": "other", "pattern": "x"})
        self.assertEqual(self.nw.exceptions_list(), [])

    def test_update_and_delete(self):
        e = self.nw.save_exception({"kind": "resource", "pattern": "a"})
        e2 = self.nw.save_exception({"id": e["id"], "kind": "resource", "pattern": "b", "note": "n"})
        self.assertEqual((e2["pattern"], e2["note"]), ("b", "n"))
        self.nw.delete_exception(e["id"])
        self.assertEqual(self.nw.exceptions_list(), [])
        with self.assertRaises(NetwatchError):
            self.nw.delete_exception(e["id"])

    def test_resolve_adds_exceptions(self):
        ev = self.flagged()
        alert = self.nw.alerts()[0]
        self.assertEqual(alert["suggest"]["behavior_pattern"], re.escape(ev["behavior"]))
        self.assertTrue(re.search(alert["suggest"]["resource_pattern"], ev["resource"]))
        out = self.nw.resolve(ev["id"], {"add_behavior": True, "behavior_pattern": alert["suggest"]["behavior_pattern"],
                                         "add_resource": True, "resource_pattern": alert["suggest"]["resource_pattern"], "note": "ok"})
        self.assertEqual({e["kind"] for e in out["exceptions"]}, {"behavior", "resource"})
        self.assertEqual(self.nw.alerts(), [])
        self.src.conns[10] = [conn("93.184.216.34", 443, laddr=("192.168.1.5", 50001))]
        self.assertTrue(self.poll()[0]["suppressed"])

    def test_resolve_dismiss_adds_nothing(self):
        ev = self.flagged()
        out = self.nw.resolve(ev["id"], {})
        self.assertEqual(out["exceptions"], [])
        self.assertEqual(self.nw.alerts(), [])
        self.assertEqual(self.nw.events_list()[0]["alert_state"], "resolved")

    def test_resolve_with_bad_pattern_changes_nothing(self):
        ev = self.flagged()
        with self.assertRaises(NetwatchError):
            self.nw.resolve(ev["id"], {"add_resource": True, "resource_pattern": "("})
        self.assertEqual(len(self.nw.alerts()), 1)
        with self.assertRaises(NetwatchError) as cm:
            self.nw.resolve("nope", {})
        self.assertEqual(cm.exception.code, "gone")


class TestSettings(_Base):
    def test_defaults_and_persistence(self):
        self.assertFalse(self.nw.settings()["enabled"])
        self.nw.update_settings({"interval_s": 5, "watch_extra_names": [" foo ", ""], "expected_folders": ["C:/x"]})
        again = NetWatch(Path(self.tmp.name), self.src, lambda ip: "", lambda: 1.0)
        self.assertEqual(again.settings()["interval_s"], 5)
        self.assertEqual(again.settings()["watch_extra_names"], ["foo"])

    def test_bad_settings(self):
        for patch in ({"nope": 1}, {"enabled": "yes"}, {"interval_s": 0}, {"interval_s": "2"}, {"expected_folders": "x"}, {"max_log_mb": -1}):
            with self.assertRaises(NetwatchError):
                self.nw.update_settings(patch)

    def test_enable_starts_and_disable_stops(self):
        self.nw.update_settings({"enabled": True, "interval_s": 0.5})
        self.assertTrue(self.nw.running)
        self.nw.update_settings({"enabled": False})
        self.assertFalse(self.nw.running)

    def test_status_coverage_honesty(self):
        st = self.nw.status()
        self.assertEqual(st["coverage"]["mode"], "connection_table")
        self.assertIn("admin", st["coverage"])
        self.assertIn("ETW", st["coverage"]["note"])
        self.assertEqual(st["geo"], "no geo database")

    def test_geo_database(self):
        path = Path(self.tmp.name) / "geo.csv"
        path.write_text("93.184.216.0/24,AS15133,US\n", encoding="utf-8")
        self.nw.update_settings({"geo_db_path": str(path)})
        self.src.conns[10] = [conn("93.184.216.34")]
        ev = self.poll()[0]
        self.assertEqual((ev["asn"], ev["country"]), ("AS15133", "US"))


class TestLogAndRegister(_Base):
    def fill(self, n=3):
        ids = []
        for i in range(n):
            self.src.conns[10] = [conn(f"93.184.216.{30 + i}")]
            ids.append(self.poll()[0]["id"])
        return ids

    def test_chain_verifies_and_detects_tamper(self):
        self.fill()
        self.assertTrue(self.nw.verify()["ok"])
        path = self.nw.log.path
        text = path.read_text(encoding="utf-8").replace("93.184.216.31", "10.9.9.9")
        path.write_text(text, encoding="utf-8")
        res = self.nw.verify()
        self.assertFalse(res["ok"])
        self.assertTrue(res["problems"])

    def test_deleted_line_breaks_chain(self):
        self.fill()
        path = self.nw.log.path
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
        self.assertFalse(self.nw.verify()["ok"])

    def test_suppressed_events_are_logged_with_exception_id(self):
        e = self.nw.save_exception({"kind": "resource", "pattern": "93"})
        self.fill(1)
        recs = [json.loads(l) for l in self.nw.log.path.read_text(encoding="utf-8").splitlines()]
        ev = [r["data"] for r in recs if r["type"] == "event"][0]
        self.assertTrue(ev["suppressed"])
        self.assertEqual(ev["suppressed_by"], e["id"])

    def test_log_reloaded(self):
        ids = self.fill()
        again = NetWatch(Path(self.tmp.name), self.src, lambda ip: "", lambda: 5000.0)
        self.assertEqual({e["id"] for e in again.events_list()}, set(ids))

    def test_delete_ids_tombstone_and_compaction(self):
        ids = self.fill(3)
        before = self.nw.log.head
        self.assertEqual(self.nw.delete_events({"ids": [ids[0], "unknown"]}), {"deleted": 1})
        self.assertEqual({e["id"] for e in self.nw.events_list()}, set(ids[1:]))
        res = self.nw.verify()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["compactions"], 1)
        recs = [json.loads(l) for l in self.nw.log.path.read_text(encoding="utf-8").splitlines()]
        kinds = [r["type"] for r in recs]
        self.assertIn("deleted", kinds)
        self.assertEqual(kinds[-1], "compacted")
        tomb = [r for r in recs if r["type"] == "deleted"][0]["data"]
        self.assertEqual(tomb["ids"], [ids[0]])
        self.assertNotIn(ids[0], json.dumps([r for r in recs if r["type"] == "event"]))
        self.assertNotEqual(recs[-1]["data"]["previous_head"], before)       # the head after the tombstone
        again = NetWatch(Path(self.tmp.name), self.src, lambda ip: "", lambda: 5000.0)
        self.assertEqual({e["id"] for e in again.events_list()}, set(ids[1:]))

    def test_delete_all_and_older_than(self):
        ids = self.fill(3)
        self.now += 86400 * 10
        self.src.conns[10] = [conn("93.184.216.99")]
        new = self.poll()[0]["id"]
        self.assertEqual(self.nw.delete_events({"older_than_days": 5})["deleted"], 3)
        self.assertEqual([e["id"] for e in self.nw.events_list()], [new])
        self.assertEqual(self.nw.delete_events({"all": True})["deleted"], 1)
        self.assertEqual(self.nw.events_list(), [])
        self.assertTrue(self.nw.verify()["ok"])
        with self.assertRaises(NetwatchError):
            self.nw.delete_events({})
        with self.assertRaises(NetwatchError):
            self.nw.delete_events({"older_than_days": "x"})

    def test_deleting_resolved_alert_drops_resolution(self):
        ids = self.fill(1)
        self.nw.resolve(ids[0], {})
        self.nw.delete_events({"ids": ids})
        self.assertTrue(self.nw.verify()["ok"])
        self.assertEqual(self.nw.alerts(), [])

    def test_manual_entry(self):
        ev = self.nw.add_manual({"process": "x.exe", "proto": "udp", "remote_ip": "8.8.8.8", "remote_port": "53", "tags": "doh_or_tor, proxy_env", "note": "seen in wireshark"})
        self.assertTrue(ev["manual"])
        self.assertEqual(ev["tags"], ["doh_or_tor", "proxy_env"])
        self.assertEqual(ev["remote_port"], 53)
        self.assertEqual(self.nw.alerts(), [])                 # a hand entry is a record, not an alert
        self.assertEqual(self.nw.events_list()[0]["id"], ev["id"])
        self.assertTrue(self.nw.verify()["ok"])
        with self.assertRaises(NetwatchError):
            self.nw.add_manual({})
        with self.assertRaises(NetwatchError):
            self.nw.add_manual({"process": "a", "remote_port": "x"})

    def test_max_log_mb_trims(self):
        self.nw.store.settings["max_log_mb"] = 0.002
        self.fill(30)
        self.assertLess(len(self.nw.events), 30)
        self.assertTrue(self.nw.verify()["ok"])

    def test_chainlog_compact_marker(self):
        log = ChainLog(Path(self.tmp.name) / "c.jsonl")
        for i in range(3):
            log.append("event", {"id": str(i)}, float(i))
        head = log.head
        log.compact(lambda r: r["data"]["id"] == "1", {"reason": "t"}, 9.0)
        self.assertTrue(log.verify()["ok"])
        self.assertEqual(log.records[-1]["data"]["previous_head"], head)
        self.assertEqual(log.records[-1]["data"]["removed_records"], 1)

    def test_export(self):
        self.fill(2)
        out = self.nw.export()
        self.assertEqual(len(out["events"]), 2)
        self.assertTrue(out["verify"]["ok"])


class NoTelemetry:
    def snapshot(self, *a, **k):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}

    def stop(self):
        pass


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = FakeSource()
        cls.now = [1000.0]
        cls.nw = NetWatch(Path(cls.tmp.name), cls.src, lambda ip: "", lambda: cls.now[0])
        cls.patch = mock.patch.object(netwatch_api, "_WATCH", cls.nw)
        cls.patch.start()
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.svc.api_key = ""
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.patch.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_round_trip(self):
        st, body = self.call("GET", "/api/netwatch")
        self.assertEqual(st, 200)
        self.assertFalse(body["enabled"])
        self.assertIn("coverage", body)
        self.assertTrue(body["psutil"])

        self.src.conns[10] = [conn("93.184.216.34")]
        self.nw.poll_once()
        st, body = self.call("GET", "/api/netwatch/alerts")
        self.assertEqual(len(body["alerts"]), 1)
        alert = body["alerts"][0]
        st, body = self.call("GET", "/api/netwatch/events?only=suspicious&limit=5")
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(self.call("GET", "/api/netwatch/events?only=bad")[0], 400)

        st, body = self.call("POST", f"/api/netwatch/alerts/{alert['id']}/resolve", {
            "add_behavior": True, "behavior_pattern": alert["suggest"]["behavior_pattern"], "add_resource": False, "note": "n"})
        self.assertEqual(st, 200)
        self.assertEqual(len(body["exceptions"]), 1)
        self.assertEqual(self.call("GET", "/api/netwatch/alerts")[1]["alerts"], [])
        self.assertEqual(self.call("POST", "/api/netwatch/alerts/nope/resolve", {})[0], 404)

        st, body = self.call("GET", "/api/netwatch/exceptions")
        eid = body["exceptions"][0]["id"]
        st, body = self.call("POST", "/api/netwatch/exceptions", {"kind": "resource", "pattern": "("})
        self.assertEqual(st, 400)
        self.assertEqual(body["error"]["code"], "bad_pattern")
        st, body = self.call("POST", "/api/netwatch/exceptions", {"id": eid, "kind": "behavior", "pattern": "x", "enabled": False})
        self.assertEqual(body["enabled"], False)
        self.assertEqual(self.call("POST", f"/api/netwatch/exceptions/{eid}/delete", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/netwatch/exceptions/{eid}/delete", {})[0], 404)

        st, body = self.call("POST", "/api/netwatch/events", {"process": "p", "remote_ip": "1.2.3.4", "remote_port": 9})
        self.assertTrue(body["manual"])
        st, body = self.call("POST", "/api/netwatch/events/delete", {"ids": [body["id"]]})
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(self.call("POST", "/api/netwatch/events/delete", {})[0], 400)

        st, body = self.call("GET", "/api/netwatch/verify")
        self.assertTrue(body["ok"])
        st, body = self.call("GET", "/api/netwatch/export")
        self.assertIn("events", body)

        st, body = self.call("POST", "/api/netwatch/settings", {"interval_s": 7})
        self.assertEqual(body["settings"]["interval_s"], 7)
        self.assertEqual(self.call("POST", "/api/netwatch/settings", {"bogus": 1})[0], 400)
        self.assertEqual(self.call("GET", "/api/netwatch/nothing")[0], 404)


if __name__ == "__main__":
    unittest.main()
