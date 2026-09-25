"""Command Builder と URL 判定の単体テスト。

依存を増やさないため標準の unittest で書いている。
    python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from app.command import build_command, build_encode_args, to_shell
from app.models import EncodeSettings, SourceSpec
from app.protocol import resolve
from app.runner import _parse_bitrate, _parse_speed, _shape_progress


class TestResolve(unittest.TestCase):
    def test_rtmp_variants_are_supported(self):
        for url in ("rtmp://host/app/key", "rtmps://host/app/key"):
            r = resolve(url)
            self.assertEqual(r.protocol, "rtmp")
            self.assertTrue(r.supported)

    def test_srt_is_supported(self):
        r = resolve("srt://host:9000?streamid=x")
        self.assertEqual(r.protocol, "srt")
        self.assertTrue(r.supported)

    def test_whip_is_supported(self):
        r = resolve("https://host/whip")
        self.assertEqual(r.protocol, "whip")
        self.assertTrue(r.supported)

    def test_https_is_whip(self):
        self.assertEqual(resolve("https://host/whip").protocol, "whip")

    def test_missing_scheme_is_reported_as_missing(self):
        # "host:1935" は urlsplit が scheme として拾ってしまうので、専用の分岐がある
        r = resolve("host:1935")
        self.assertIsNone(r.protocol)
        self.assertIn("スキームがありません", r.reason)

    def test_empty(self):
        self.assertFalse(resolve("").supported)


class TestCommandBuilder(unittest.TestCase):
    def build(self, **kwargs):
        params = dict(
            ffmpeg_path="ffmpeg",
            protocol="rtmp",
            target_url="rtmp://example.com/live/key",
            source=SourceSpec(),
            encode=EncodeSettings(),
            duration_sec=None,
        )
        params.update(kwargs)
        return build_command(**params)

    def test_gop_is_converted_to_frames(self):
        argv = self.build(encode=EncodeSettings(fps=30, gop_sec=2.0))
        self.assertEqual(argv[argv.index("-g") + 1], "60")

        argv = self.build(encode=EncodeSettings(fps=60, gop_sec=1.0))
        self.assertEqual(argv[argv.index("-g") + 1], "60")

    def test_bufsize_defaults_to_double_bitrate(self):
        argv = self.build(encode=EncodeSettings(bitrate_kbps=3000))
        self.assertEqual(argv[argv.index("-bufsize") + 1], "6000k")

    def test_output_is_flv_for_rtmp(self):
        argv = self.build()
        self.assertEqual(argv[-3:], ["-f", "flv", "rtmp://example.com/live/key"])

    def test_duration_zero_means_unlimited(self):
        self.assertNotIn("-t", self.build(duration_sec=0))
        self.assertIn("-t", self.build(duration_sec=30))

    def test_videotoolbox_omits_libx264_only_flags(self):
        args = build_encode_args(EncodeSettings(video_encoder="h264_videotoolbox"), SourceSpec())
        for flag in ("-preset", "-sc_threshold", "-bf"):
            self.assertNotIn(flag, args)

    def test_without_tone_disables_audio(self):
        argv = self.build(source=SourceSpec(with_tone=False))
        self.assertIn("-an", argv)
        self.assertNotIn("-c:a", argv)

    def test_unknown_protocol_raises(self):
        with self.assertRaises(ValueError):
            self.build(protocol="rtsp")

    def test_shell_quoting_survives_special_characters(self):
        argv = self.build(target_url="rtmp://example.com/live/key?a=1&b=2")
        self.assertIn("'rtmp://example.com/live/key?a=1&b=2'", to_shell(argv))


class TestProgressParsing(unittest.TestCase):
    def test_shape_progress(self):
        block = {
            "frame": "150", "fps": "30.0", "bitrate": "2500.4kbits/s",
            "total_size": "1024", "out_time_us": "5000000",
            "dup_frames": "0", "drop_frames": "3", "speed": "1.01x",
            "progress": "continue",
        }
        p = _shape_progress(block)
        self.assertEqual(p["frame"], 150)
        self.assertEqual(p["out_time_sec"], 5.0)
        self.assertEqual(p["drop_frames"], 3)
        self.assertAlmostEqual(p["speed"], 1.01)

    def test_na_values_do_not_crash(self):
        self.assertEqual(_parse_bitrate("N/A"), 0.0)
        self.assertEqual(_parse_speed("N/A"), 0.0)
        p = _shape_progress({"frame": "N/A", "progress": "continue"})
        self.assertEqual(p["frame"], 0)


class TestSrtParsing(unittest.TestCase):
    def parse(self, url):
        return resolve(url)

    def params(self, res):
        return {p.key: p.value for p in res.params}

    def test_srt_is_supported_and_decomposed(self):
        res = self.parse(
            "srt://ingest.example.com:9000?streamid=publish/live/abc"
            "&latency=200000&passphrase=hunter2secret&pbkeylen=32"
        )
        self.assertEqual(res.protocol, "srt")
        self.assertTrue(res.supported)
        p = self.params(res)
        self.assertEqual(p["host"], "ingest.example.com")
        self.assertEqual(p["port"], "9000")
        self.assertEqual(p["streamid"], "publish/live/abc")
        self.assertEqual(p["pbkeylen"], "32")

    def test_passphrase_is_marked_secret_and_masked_in_output(self):
        res = self.parse("srt://h:9000?passphrase=hunter2secret&pbkeylen=16")
        self.assertEqual(res.secrets["passphrase"], "hunter2secret")
        rendered = [p for p in res.as_dict()["params"] if p["key"] == "passphrase"][0]
        self.assertNotIn("hunter2secret", rendered["value"])

    def test_latency_in_milliseconds_is_warned(self):
        # FFmpeg の srt latency はマイクロ秒。200 と書くと 0.2ms になる
        res = self.parse("srt://h:9000?latency=200")
        self.assertTrue(any("200000" in w for w in res.warnings))

        res = self.parse("srt://h:9000?latency=200000")
        self.assertFalse(any("マイクロ秒" in w for w in res.warnings))

    def test_missing_port_is_warned(self):
        self.assertTrue(any("ポート" in w for w in self.parse("srt://h").warnings))

    def test_listener_mode_is_warned(self):
        self.assertTrue(any("caller" in w for w in self.parse("srt://h:9000?mode=listener").warnings))

    def test_passphrase_without_pbkeylen_is_warned(self):
        self.assertTrue(any("pbkeylen" in w for w in self.parse("srt://h:9000?passphrase=x").warnings))


class TestQueryParsing(unittest.TestCase):
    def test_plus_is_not_turned_into_a_space(self):
        from app.protocol import parse_query

        # FFmpeg の av_find_info_tag は URL デコードしないので、+ はそのまま送られる。
        # 表示だけ空白になると、実際に送る値と食い違って切り分けを誤らせる
        pairs, _ = parse_query("streamid=as+d69f229c-d285")
        self.assertEqual(pairs, [("streamid", "as+d69f229c-d285")])

    def test_percent_encoding_is_left_as_is(self):
        from app.protocol import parse_query

        pairs, _ = parse_query("passphrase=a%40b")
        self.assertEqual(pairs[0][1], "a%40b")

    def test_empty_segment_is_reported(self):
        from app.protocol import parse_query

        pairs, issues = parse_query("a=1&&b=2")
        self.assertEqual(pairs, [("a", "1"), ("b", "2")])
        self.assertTrue(issues)

    def test_trailing_ampersand_is_reported(self):
        from app.protocol import parse_query

        self.assertTrue(parse_query("a=1&")[1])

    def test_clean_url_removes_empty_segments(self):
        from app.protocol import clean_query_url

        self.assertEqual(
            clean_query_url("srt://h:9000?a=1&&b=2&"),
            "srt://h:9000?a=1&b=2",
        )

    def test_clean_url_preserves_plus(self):
        from app.protocol import clean_query_url

        self.assertIn("as+d69f", clean_query_url("srt://h:9000?streamid=as+d69f&&x=1"))


class TestEmptyQueryDetection(unittest.TestCase):
    def test_srt_warns_and_suggests_a_fix(self):
        url = ("srt://h:8889?streamid=as+d69f&pbkeylen=32&&passphrase=secret1234")
        r = resolve(url)
        self.assertTrue(any("空の項目" in w for w in r.warnings))
        self.assertNotIn("&&", r.suggested_url)
        # 提案URLでは警告が消えること
        self.assertFalse(any("空の項目" in w for w in resolve(r.suggested_url).warnings))

    def test_streamid_is_displayed_exactly_as_sent(self):
        r = resolve("srt://h:8889?streamid=as+d69f229c")
        streamid = next(p for p in r.params if p.key == "streamid")
        self.assertEqual(streamid.value, "as+d69f229c")

    def test_rtmp_and_whip_are_checked_too(self):
        self.assertTrue(resolve("rtmp://h/live/key?a=1&&b=2").warnings)
        self.assertTrue(resolve("https://h/whip?a=1&&b=2").warnings)

    def test_healthy_url_has_no_suggestion(self):
        self.assertEqual(resolve("srt://h:9000?streamid=abc").suggested_url, "")


class TestSrtReachabilityProbe(unittest.TestCase):
    def test_induction_packet_shape(self):
        from app.srtprobe import HS_TYPE_INDUCTION, _induction_packet
        import struct

        pkt = _induction_packet()
        # SRT の制御パケットは 16バイトヘッダ + 48バイトのハンドシェイクCIF
        self.assertEqual(len(pkt), 64)
        self.assertTrue(pkt[0] & 0x80, "制御パケットのビットが立っていない")
        hs_type = struct.unpack("!i", pkt[16 + 20:16 + 24])[0]
        self.assertEqual(hs_type, HS_TYPE_INDUCTION)

    def test_no_listener_reports_no_response(self):
        from app.srtprobe import probe

        r = probe("127.0.0.1", 19599, timeout=0.4)
        self.assertFalse(r.responded)

    def test_explain_separates_the_two_causes(self):
        from app.srtprobe import SrtProbeResult, explain

        no_answer = explain(SrtProbeResult(False, 3000, "タイムアウト"), "h", 9000)
        self.assertIn("無関係", no_answer)          # streamid の話ではないと言い切る

        answered = explain(SrtProbeResult(True, 40, "応答"), "h", 9000)
        self.assertIn("streamid", answered)         # こちらは認証情報を疑わせる


class TestProbeFailureClassification(unittest.TestCase):
    def test_connection_failure_is_not_treated_as_a_drop(self):
        from app.probe import _connected_then_dropped

        self.assertFalse(_connected_then_dropped(
            "[srt @ 0x1] Connection to srt://h:8889?streamid=x failed: Input/output error"
        ))

    def test_mid_stream_drop_is_detected(self):
        from app.probe import _connected_then_dropped

        self.assertTrue(_connected_then_dropped(
            "[vost#0:0/libx264 @ 0x1] Error submitting a packet to the muxer: Input/output error"
        ))

    def test_clean_log_is_neither(self):
        from app.probe import _connected_then_dropped

        self.assertFalse(_connected_then_dropped(""))


class TestRtmpParsing(unittest.TestCase):
    def test_stream_key_is_extracted_and_secret(self):
        res = resolve("rtmp://live.example.com/app/secretkey123")
        self.assertEqual(res.secrets["stream_key"], "secretkey123")
        self.assertNotIn("secretkey123", res.as_dict()["params"].__str__())

    def test_single_segment_is_not_treated_as_key(self):
        res = resolve("rtmp://live.example.com/live")
        self.assertNotIn("stream_key", res.secrets)
        self.assertTrue(res.warnings)


class TestSrtCommand(unittest.TestCase):
    def test_srt_uses_mpegts_and_passes_url_verbatim(self):
        url = "srt://h:9000?streamid=a&latency=200000"
        argv = build_command(
            ffmpeg_path="ffmpeg", protocol="srt", target_url=url,
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=None,
        )
        self.assertEqual(argv[-3:], ["-f", "mpegts", url])


class TestMasking(unittest.TestCase):
    def test_mask_values_hides_every_occurrence(self):
        from app.secrets import mask_values

        secret = "passphrase-value"
        text = f"url=srt://h:9000?passphrase={secret} and again {secret}"
        self.assertNotIn(secret, mask_values(text, [secret]))

    def test_short_values_are_left_alone_in_free_text(self):
        from app.secrets import MIN_MASKABLE_LEN, mask_values

        # 短い値を自由文で無差別に置換すると、無関係な箇所を壊す。
        # 短い秘匿値は URL 単位の置換（mask_url / redact）が担当する
        self.assertLess(len("x"), MIN_MASKABLE_LEN)
        self.assertEqual(mask_values("drawtext=x", ["x"]), "drawtext=x")

    def test_longer_secret_masked_first(self):
        from app.secrets import mask_values

        # 短い値が長い値の一部でも、断片が残らないこと
        out = mask_values("key=abcdefghij", ["abcdefgh", "abcdefghij"])
        self.assertNotIn("abcdefgh", out)

    def test_empty_values_are_ignored(self):
        from app.secrets import mask_values

        self.assertEqual(mask_values("hello", ["", None]), "hello")


class TestEncodeNormalization(unittest.TestCase):
    def test_defaulted_and_explicit_values_hash_the_same(self):
        a = EncodeSettings(bitrate_kbps=4500)
        b = EncodeSettings(bitrate_kbps=4500, maxrate_kbps=4500, bufsize_kbps=9000)
        self.assertEqual(a.settings_hash(), b.settings_hash())

    def test_different_settings_hash_differently(self):
        self.assertNotEqual(
            EncodeSettings(bitrate_kbps=4500).settings_hash(),
            EncodeSettings(bitrate_kbps=4600).settings_hash(),
        )

    def test_passthrough_ignores_video_parameters(self):
        a = EncodeSettings(mode="passthrough", bitrate_kbps=1000)
        b = EncodeSettings(mode="passthrough", bitrate_kbps=9000)
        self.assertEqual(a.settings_hash(), b.settings_hash())

    def test_auto_name(self):
        self.assertEqual(
            EncodeSettings(height=720, fps=30, bitrate_kbps=2500, gop_sec=1.0).auto_name(),
            "720p30 · 2500k · H.264 · GOP1s",
        )
        self.assertIn("VT", EncodeSettings(video_encoder="h264_videotoolbox").auto_name())


class TestTemplateStore(unittest.TestCase):
    def setUp(self):
        import sqlite3

        from app.db import SCHEMA_V3
        from app.templates import TemplateStore

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_V3)
        self.store = TemplateStore(self.conn)
        self.store.seed_builtins()

    def tearDown(self):
        self.conn.close()

    def test_builtins_are_seeded_once(self):
        first = len(self.store.list())
        self.store.seed_builtins()
        self.assertEqual(len(self.store.list()), first)
        self.assertTrue(all(t["is_builtin"] for t in self.store.list()))

    def test_using_a_builtin_setting_does_not_create_a_duplicate(self):
        before = len(self.store.list())
        used = self.store.record_use(EncodeSettings(width=1280, height=720, bitrate_kbps=2500))
        self.assertEqual(len(self.store.list()), before)
        self.assertEqual(used["name"], "Low 720p30")
        self.assertEqual(used["use_count"], 1)

    def test_new_settings_are_auto_saved_and_deduped(self):
        enc = EncodeSettings(bitrate_kbps=1234)
        a = self.store.record_use(enc)
        b = self.store.record_use(enc)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["use_count"], 2)
        self.assertTrue(a["is_auto_named"])
        self.assertIn("1234k", a["name"])

    def test_rename_clears_auto_named_flag(self):
        t = self.store.record_use(EncodeSettings(bitrate_kbps=1111))
        renamed = self.store.patch(t["id"], name="本番相当", is_pinned=None)
        self.assertEqual(renamed["name"], "本番相当")
        self.assertFalse(renamed["is_auto_named"])

    def test_blank_rename_is_rejected(self):
        t = self.store.record_use(EncodeSettings(bitrate_kbps=1112))
        with self.assertRaises(ValueError):
            self.store.patch(t["id"], name="  ", is_pinned=None)

    def test_builtin_cannot_be_deleted(self):
        builtin = next(t for t in self.store.list() if t["is_builtin"])
        ok, reason = self.store.delete(builtin["id"])
        self.assertFalse(ok)
        self.assertIn("ビルトイン", reason)

    def test_lru_keeps_pinned_and_renamed_but_drops_old_auto(self):
        from app.templates import LRU_KEEP

        keep_pinned = self.store.record_use(EncodeSettings(bitrate_kbps=100))
        self.store.patch(keep_pinned["id"], name=None, is_pinned=True)
        keep_named = self.store.record_use(EncodeSettings(bitrate_kbps=101))
        self.store.patch(keep_named["id"], name="残すやつ", is_pinned=None)

        for i in range(LRU_KEEP + 5):
            self.store.record_use(EncodeSettings(bitrate_kbps=1000 + i))

        ids = {t["id"] for t in self.store.list()}
        self.assertIn(keep_pinned["id"], ids)
        self.assertIn(keep_named["id"], ids)

        # LRU の対象は「ビルトインでない・ピン留めでない・自動命名のまま」のものだけ
        prunable = [
            t for t in self.store.list()
            if not t["is_builtin"] and not t["is_pinned"] and t["is_auto_named"]
        ]
        self.assertEqual(len(prunable), LRU_KEEP)

    def test_last_used_returns_most_recent(self):
        # 秒精度のタイムスタンプでは同着になるので、続けて呼んでも壊れないこと
        self.store.record_use(EncodeSettings(bitrate_kbps=201))
        latest = self.store.record_use(EncodeSettings(bitrate_kbps=202))
        self.assertEqual(self.store.last_used()["id"], latest["id"])

    def test_list_is_ordered_pinned_then_recent(self):
        old = self.store.record_use(EncodeSettings(bitrate_kbps=301))
        new = self.store.record_use(EncodeSettings(bitrate_kbps=302))
        self.assertEqual(self.store.list()[0]["id"], new["id"])

        self.store.patch(old["id"], name=None, is_pinned=True)
        self.assertEqual(self.store.list()[0]["id"], old["id"])


class TestPassthroughCommand(unittest.TestCase):
    def test_passthrough_copies_streams(self):
        argv = build_command(
            ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://example.com/live/key",
            source=SourceSpec(), encode=EncodeSettings(mode="passthrough"), duration_sec=None,
        )
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertNotIn("-b:v", argv)


class TestOverlay(unittest.TestCase):
    def build(self, overlay=None, **kwargs):
        from app.models import OverlaySettings
        from app.overlay import build_filters

        params = dict(font_path="/tmp/f.ttf", protocol="srt",
                      encode=EncodeSettings(), job_label="")
        params.update(kwargs)
        return build_filters(overlay or OverlaySettings(), **params)

    def test_both_mode_draws_three_layers(self):
        from app.models import OverlaySettings

        # 壁時計 + フレーム番号 + 情報行
        self.assertEqual(len(self.build(OverlaySettings(clock_mode="both"))), 3)
        self.assertEqual(len(self.build(OverlaySettings(clock_mode="wallclock"))), 2)
        self.assertEqual(len(self.build(OverlaySettings(clock_mode="framecount"))), 2)

    def test_disabled_overlay_produces_nothing(self):
        from app.models import OverlaySettings

        self.assertEqual(
            self.build(OverlaySettings(clock_enabled=False, info_enabled=False)), []
        )

    def test_second_clock_line_is_offset(self):
        from app.models import OverlaySettings

        filters = self.build(OverlaySettings(clock_mode="both", clock_position="top-center"))
        self.assertNotIn("1.4", filters[0])   # 1行目はずらさない
        self.assertIn("1.4", filters[1])      # 2行目は1行分下へ

    def test_bottom_position_offsets_upward(self):
        from app.models import OverlaySettings

        filters = self.build(OverlaySettings(clock_mode="both", clock_position="bottom-left"))
        self.assertIn("h-th-", filters[1])
        self.assertIn("- (", filters[1])      # 下寄せは上方向にずらす

    def test_info_text_is_generated_from_actual_settings(self):
        from app.overlay import build_info_text

        text = build_info_text(
            ["protocol", "resolution", "codec", "bitrate", "gop", "preset", "job"],
            protocol="rtmp",
            encode=EncodeSettings(width=1280, height=720, fps=60, bitrate_kbps=6000, gop_sec=1.0),
            job_label="demo",
        )
        self.assertEqual(text, "RTMP | 1280x720@60 | H.264 | 6000k | GOP 1s | veryfast | demo")

    def test_info_items_can_be_narrowed(self):
        from app.overlay import build_info_text

        text = build_info_text(["protocol", "bitrate"], protocol="srt", encode=EncodeSettings())
        self.assertEqual(text, "SRT | 4500k")

    def test_job_label_is_skipped_when_empty(self):
        from app.overlay import build_info_text

        text = build_info_text(["protocol", "job"], protocol="srt", encode=EncodeSettings())
        self.assertEqual(text, "SRT")

    def test_escape_neutralises_filtergraph_metacharacters(self):
        from app.overlay import escape_text

        out = escape_text("a:b'c%d\\e")
        for raw in (":", "'", "%"):
            self.assertNotIn(f"{raw}", out.replace(f"\\{raw}", ""))

    def test_expansions_are_not_escaped(self):
        from app.overlay import FRAMECOUNT, WALLCLOCK

        # %{...} は展開させたいので、エスケープを通してはいけない
        self.assertIn("%{localtime}", WALLCLOCK)
        self.assertIn("%{eif", FRAMECOUNT)


class TestOverlayInCommand(unittest.TestCase):
    def build(self, **kwargs):
        from app.models import OverlaySettings

        params = dict(
            ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://example.com/live/key",
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=None,
            overlay=OverlaySettings(), font_path="/tmp/f.ttf",
        )
        params.update(kwargs)
        return build_command(**params)

    def test_filtergraph_is_a_single_argv_element(self):
        argv = self.build()
        vf = argv[argv.index("-filter_complex") + 1]
        self.assertEqual(vf.count("drawtext="), 3)
        self.assertNotIn(" -", vf.split(":")[0])   # 引数が分割されていない

    def test_no_font_means_no_overlay(self):
        argv = self.build(font_path="")
        self.assertNotIn("drawtext", argv[argv.index("-filter_complex") + 1])

    def test_passthrough_never_gets_overlay(self):
        argv = self.build(encode=EncodeSettings(mode="passthrough"))
        self.assertNotIn("-filter_complex", argv)

    def test_overlay_comes_before_encoder_args(self):
        argv = self.build()
        self.assertLess(argv.index("-filter_complex"), argv.index("-c:v"))

    def test_realtime_is_applied_before_the_overlay(self):
        # 待ってから時計を焼く。逆にすると焼いた時刻と送出時刻がずれる
        vf = self.build()[self.build().index("-filter_complex") + 1]
        self.assertLess(vf.index("realtime"), vf.index("drawtext"))


class TestRedaction(unittest.TestCase):
    def test_short_secret_does_not_corrupt_free_text(self):
        from app.secrets import redact

        # ストリームキーが "x" でも drawtext が壊れないこと（実際に踏んだ事故）
        cmd = "ffmpeg -vf drawtext=text=hello -f flv rtmp://h/live/x"
        out = redact(cmd, url="rtmp://h/live/x", display_url="rtmp://h/live/●●●●●●", values=["x"])
        self.assertIn("drawtext=text=hello", out)
        self.assertNotIn("/live/x", out)

    def test_url_masking_has_no_length_floor(self):
        from app.secrets import mask_url

        self.assertNotIn("/live/x", mask_url("rtmp://h/live/x", ["x"]))

    def test_long_secret_is_masked_in_free_text(self):
        from app.secrets import mask_values

        self.assertNotIn("supersecretkey", mask_values("key=supersecretkey", ["supersecretkey"]))


class TestSourceInputs(unittest.TestCase):
    def build(self, source, **kwargs):
        params = dict(
            ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://example.com/live/key",
            source=source, encode=EncodeSettings(width=1280, height=720, fps=30),
            duration_sec=None,
        )
        params.update(kwargs)
        return build_command(**params)

    def test_testsrc_is_paced_by_the_realtime_filter(self):
        # -re ではなく realtime フィルタで絞る。-re は映像と音声を同時に符号化すると
        # 間欠的に 0.81x へ落ちる（実測）
        argv = self.build(SourceSpec())
        self.assertNotIn("-re", argv)
        self.assertIn("realtime", argv[argv.index("-filter_complex") + 1])

    def test_audio_is_never_paced_on_its_own(self):
        # 映像は realtime、音声は arealtime と別々の時計で絞ると、映像側が詰まった
        # ときに音声だけが先へ進む。実測では映像のフレーム生成が128秒間止まった
        for source in (
            SourceSpec(),
            SourceSpec(type="live_url", url="https://e.com/a.m3u8", pace="realtime"),
        ):
            with self.subTest(source=source.type):
                argv = self.build(source)
                af = argv[argv.index("-af") + 1] if "-af" in argv else ""
                self.assertNotIn("arealtime", af)

    def test_audio_is_not_written_ahead_of_video(self):
        # 既定の10秒を超えると FFmpeg は相手を待たずに書く。実測では映像を1枚も
        # 挟まずに117秒ぶんの音声を送出していた。0 は「追いつくまで待つ」
        argv = self.build(SourceSpec())
        self.assertEqual(argv[argv.index("-max_interleave_delta") + 1], "0")
        # 出力側のオプションなので、末尾の "-f <muxer> <url>" より前に来る
        # （先頭の -f は testsrc の "-f lavfi" なので、探すのは最後の -f）
        self.assertLess(argv.index("-max_interleave_delta"), len(argv) - 1 - argv[::-1].index("-f"))

    def test_true_live_source_is_not_paced(self):
        # 入力がすでに実時間で来ているので、さらに絞ると二重に待つことになる
        argv = self.build(
            SourceSpec(type="live_url", url="https://example.com/a.m3u8", pace="asfast")
        )
        self.assertNotIn("-re", argv)
        self.assertNotIn("realtime", argv[argv.index("-filter_complex") + 1])
        self.assertIn("-reconnect", argv)

    def test_vod_source_is_paced(self):
        # VOD を実時間で絞らないと、取れるだけ取って数十倍速で送出してしまう
        argv = self.build(
            SourceSpec(type="live_url", url="https://example.com/a.m3u8", pace="realtime")
        )
        self.assertIn("realtime", argv[argv.index("-filter_complex") + 1])

    def test_passthrough_falls_back_to_re(self):
        # -c copy はフィルタを通らないので、入力側で絞るしかない
        argv = self.build(SourceSpec(type="file", path="a.mp4", loop=False),
                          encode=EncodeSettings(mode="passthrough"), media_dir=self._tmpdir())
        self.assertIn("-re", argv)

    def _tmpdir(self):
        import tempfile
        from pathlib import Path

        d = tempfile.mkdtemp()
        Path(d, "a.mp4").write_bytes(b"x")
        return d

    def test_delivery_side_burst_is_never_used(self):
        # -readrate_initial_burst は読んだぶんをそのまま送り出すので下流に積み上がり、
        # 焼き込んだ時計で測る遅延が増える。素材の貯金は入力側で作る
        argv = self.build(
            SourceSpec(
                type="live_url", url="https://example.com/a.m3u8",
                pace="realtime", prebuffer_sec=5,
            )
        )
        self.assertNotIn("-readrate_initial_burst", argv)

    def test_prebuffer_reads_from_behind_the_live_edge(self):
        argv = self.build(
            SourceSpec(
                type="live_url", url="https://example.com/a.m3u8",
                pace="realtime", start_index=5,
            )
        )
        self.assertEqual(argv[argv.index("-live_start_index") + 1], "-5")

    def test_no_prebuffer_means_no_start_index(self):
        argv = self.build(
            SourceSpec(type="live_url", url="https://example.com/a.m3u8", pace="realtime")
        )
        self.assertNotIn("-live_start_index", argv)

    def test_unknown_source_falls_back_to_realtime(self):
        # 判定できないときは安全側。バーストさせるより遅延したほうがまし
        from unittest.mock import patch

        from app.sources import resolve_pace

        with patch("app.sources.is_live_stream", return_value=None):
            paced, why = resolve_pace(
                SourceSpec(type="live_url", url="https://example.com/a.m3u8")
            )
        self.assertTrue(paced)
        self.assertIn("安全側", why)

    def test_live_url_reconnect_can_be_disabled(self):
        argv = self.build(
            SourceSpec(type="live_url", url="https://e.com/a.m3u8", reconnect=False, pace="asfast")
        )
        self.assertNotIn("-reconnect", argv)

    def test_file_loop_uses_stream_loop_before_input(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.mp4").write_bytes(b"x")
            argv = self.build(SourceSpec(type="file", path="a.mp4"), media_dir=d)
            self.assertLess(argv.index("-stream_loop"), argv.index("-i"))
            self.assertEqual(argv[argv.index("-stream_loop") + 1], "-1")

    def test_file_loop_rewrites_timestamps(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.mp4").write_bytes(b"x")
            argv = self.build(SourceSpec(type="file", path="a.mp4"), media_dir=d)
            vf = argv[argv.index("-filter_complex") + 1]
            af = argv[argv.index("-af") + 1]
            # ループ境界で PTS が巻き戻るのを防ぐ
            self.assertIn("setpts=N/FRAME_RATE/TB", vf)
            self.assertIn("asetpts=N/SR/TB", af)
            # fps= より後ろに置かないと N が固定フレームレートの連番にならない
            self.assertLess(vf.index("fps=30"), vf.index("setpts=N/FRAME_RATE/TB"))

    def test_file_without_loop_skips_timestamp_rewrite(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.mp4").write_bytes(b"x")
            argv = self.build(SourceSpec(type="file", path="a.mp4", loop=False), media_dir=d)
            # ループしないなら音声に手当ては要らない。-af 自体が付かない
            self.assertNotIn("-af", argv)
            self.assertNotIn("-stream_loop", argv)

    def test_file_and_live_are_scaled_and_padded(self):
        argv = self.build(SourceSpec(type="live_url", url="https://e.com/a.m3u8", pace="asfast"))
        vf = argv[argv.index("-filter_complex") + 1]
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("pad=1280:720", vf)

    def test_audio_mapping_is_optional_for_real_sources(self):
        # 音声トラックが無いファイルでも落ちないように "?" を付ける
        argv = self.build(SourceSpec(type="live_url", url="https://e.com/a.m3u8", pace="asfast"))
        self.assertIn("0:a:0?", argv)


class TestMediaPathSafety(unittest.TestCase):
    def test_traversal_is_rejected(self):
        import tempfile

        from app.command import resolve_media_path

        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                resolve_media_path("../../etc/passwd", d)

    def test_absolute_path_escapes_are_rejected(self):
        import tempfile

        from app.command import resolve_media_path

        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                resolve_media_path("/etc/passwd", d)

    def test_missing_file_is_reported(self):
        import tempfile

        from app.command import resolve_media_path

        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                resolve_media_path("nope.mp4", d)

    def test_upload_name_is_flattened_and_checked(self):
        from pathlib import Path

        from app.sources import safe_upload_path

        base = Path("/tmp/media")
        self.assertEqual(safe_upload_path(base, "../evil.mp4"), base / "evil.mp4")
        with self.assertRaises(ValueError):
            safe_upload_path(base, "script.sh")
        with self.assertRaises(ValueError):
            safe_upload_path(base, ".hidden.mp4")


class TestCatalog(unittest.TestCase):
    def test_every_entry_has_the_required_fields(self):
        from app.sources import CATALOG

        for c in CATALOG:
            for key in ("id", "name", "url", "live", "note"):
                self.assertIn(key, c, f"{c.get('id')} に {key} がありません")
            self.assertTrue(c["url"].startswith("https://"), c["id"])
            self.assertIsInstance(c["live"], bool)

    def test_ids_are_unique(self):
        from app.sources import CATALOG

        ids = [c["id"] for c in CATALOG]
        self.assertEqual(len(ids), len(set(ids)))

    def test_has_enough_always_on_sources(self):
        from app.sources import CATALOG

        # 長時間の耐久試験に使えるものが複数必要
        self.assertGreaterEqual(sum(1 for c in CATALOG if c["live"]), 3)


class TestErrorTranslation(unittest.TestCase):
    def test_rtmp_connection_failure(self):
        from app.errors import diagnose_line

        d = diagnose_line("[tcp @ 0x1] Connection to tcp://h:1935 failed: Connection refused", "rtmp")
        self.assertIsNotNone(d)
        self.assertEqual(d.severity, "error")

    def test_srt_passphrase_mismatch(self):
        from app.errors import diagnose_line

        d = diagnose_line("[srt] KM REFUSED", "srt")
        self.assertIn("passphrase", d.message)

    def test_protocol_scoped_rule_does_not_leak(self):
        from app.errors import diagnose_line

        line = "Connection to tcp://h:1935 failed"
        self.assertIsNotNone(diagnose_line(line, "rtmp"))
        # RTMP 専用の説明を SRT のジョブに出さない
        d = diagnose_line(line, "srt")
        self.assertTrue(d is None or "RTMP" not in d.message)

    def test_unknown_line_is_not_forced_into_a_diagnosis(self):
        from app.errors import diagnose_line

        self.assertIsNone(diagnose_line("frame= 100 fps=30", "rtmp"))

    def test_slow_encode_is_flagged_from_progress(self):
        from app.errors import diagnose_progress

        found = diagnose_progress({"speed": 0.72, "drop_frames": 0})
        self.assertEqual([d.key for d in found], ["slow_encode"])

    def test_healthy_progress_reports_nothing(self):
        from app.errors import diagnose_progress

        self.assertEqual(diagnose_progress({"speed": 1.0, "drop_frames": 0}), [])

    def test_dropped_frames_are_flagged(self):
        from app.errors import diagnose_progress

        found = diagnose_progress({"speed": 1.0, "drop_frames": 12})
        self.assertEqual([d.key for d in found], ["dropping"])


class TestInstantaneousRate(unittest.TestCase):
    """FFmpeg の speed は累積平均なので、今の状態を表さない。

    実際に瞬間 0.79x で遅れ続けているのに累積は 0.91 を示し、
    閾値 0.9 を超えているせいで警告が出ていなかった。
    """

    def runner(self):
        from app.runner import JobRunner

        from collections import deque

        from app.runner import RATE_WINDOW_SAMPLES

        r = JobRunner.__new__(JobRunner)
        r._rate_window = deque(maxlen=RATE_WINDOW_SAMPLES)
        return r

    def test_needs_several_samples_first(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 1.0]):
            self.assertIsNone(r._instant_rate({"out_time_sec": 10.0}))
            self.assertIsNone(r._instant_rate({"out_time_sec": 11.0}))

    def test_rate_is_measured_over_the_window(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 5.0, 10.0]):
            r._instant_rate({"out_time_sec": 100.0})
            r._instant_rate({"out_time_sec": 104.0})
            rate = r._instant_rate({"out_time_sec": 108.0})
        self.assertAlmostEqual(rate, 0.8)      # 壁10秒で出力8秒 = 0.8x

    def test_window_smooths_a_single_bad_sample(self):
        from unittest.mock import patch

        r = self.runner()
        # 1サンプルだけ取りこぼしても、窓で均されて 1.0 付近に留まる
        with patch("app.runner.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]):
            r._instant_rate({"out_time_sec": 0.0})
            r._instant_rate({"out_time_sec": 1.0})
            r._instant_rate({"out_time_sec": 1.0})
            rate = r._instant_rate({"out_time_sec": 3.0})
        self.assertGreater(rate, 0.9)

    def test_catching_up_shows_above_one(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 5.0, 10.0]):
            r._instant_rate({"out_time_sec": 0.0})
            r._instant_rate({"out_time_sec": 6.0})
            self.assertAlmostEqual(r._instant_rate({"out_time_sec": 12.0}), 1.2)

    def test_rewound_out_time_does_not_produce_a_negative_rate(self):
        from unittest.mock import patch

        r = self.runner()
        # 再接続などで out_time が巻き戻ると、素直に割ると負の値になる。
        # 負のレートは平均を壊すうえ、「0 < speed」の判定をすり抜けて
        # 警告まで止めてしまう
        with patch("app.runner.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0]):
            r._instant_rate({"out_time_sec": 100.0})
            r._instant_rate({"out_time_sec": 101.0})
            r._instant_rate({"out_time_sec": 102.0})
            self.assertIsNone(r._instant_rate({"out_time_sec": 0.0}))   # 巻き戻り
            self.assertIsNone(r._instant_rate({"out_time_sec": 1.0}))   # 窓の作り直し中

    def test_rate_never_goes_negative(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 1.0, 2.0]):
            for out in (10.0, 11.0, 12.0):
                rate = r._instant_rate({"out_time_sec": out})
        self.assertGreaterEqual(rate, 0)

    def test_too_close_samples_are_ignored(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 0.01, 0.02]):
            for _ in range(3):
                last = r._instant_rate({"out_time_sec": 0.0})
        self.assertIsNone(last)


class TestProgressHealth(unittest.TestCase):
    def health(self):
        from app.errors import ProgressHealth

        return ProgressHealth()

    def feed(self, h, speeds, drops=0):
        out = []
        for sp in speeds:
            out.append(h.observe({"speed": sp, "drop_frames": drops}))
        return out

    def test_instant_rate_wins_over_cumulative_speed(self):
        # 累積 0.91（閾値超え）でも、瞬間 0.79 なら遅れとして扱う
        h = self.health()
        for _ in range(4):
            h.observe({"speed": 0.91, "rate": 1.0, "drop_frames": 0})
        found = []
        for _ in range(3):
            f, _ = h.observe({"speed": 0.91, "rate": 0.79, "drop_frames": 0})
            found += f
        self.assertEqual([f.key for f in found], ["slow_encode"])

    def test_startup_dip_is_ignored(self):
        # -re では speed が 1.0 で頭打ちになるため、揺らぎは必ず下振れになる。
        # 起動直後の落ち込みで警告を出してはいけない
        h = self.health()
        results = self.feed(h, [1.59, 0.59, 0.82, 1.11])
        self.assertTrue(all(not found for found, _ in results))

    def test_slow_message_names_both_causes(self):
        # 原因を決めつけない。エンコードが重いのか、送出が詰まっているのかは
        # 別物で、対処も違う（実際に後者だった）
        h = self.health()
        self.feed(h, [1.0] * 4)
        found = [f for found, _ in self.feed(h, [0.8, 0.8, 0.8]) for f in found]
        msg = found[0].message
        self.assertIn("エンコードが重い", msg)
        self.assertIn("送出先への書き込みが詰まっている", msg)

    def test_sustained_slowness_is_reported_once(self):
        h = self.health()
        self.feed(h, [1.0, 1.0, 1.0, 1.0])          # 猶予を消化
        results = self.feed(h, [0.5, 0.5, 0.5, 0.5])
        reported = [f for found, _ in results for f in found]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0].key, "slow_encode")

    def test_recovery_resolves_the_warning(self):
        h = self.health()
        self.feed(h, [1.0] * 4 + [0.5, 0.5, 0.5])
        _, resolved = h.observe({"speed": 1.0, "drop_frames": 0})
        self.assertEqual(resolved, ["slow_encode"])

    def test_single_dip_after_warning_does_not_double_report(self):
        h = self.health()
        self.feed(h, [1.0] * 4 + [0.5, 0.5, 0.5])
        again = self.feed(h, [0.5, 0.5])
        self.assertTrue(all(not found for found, _ in again))

    def test_drops_are_reported_by_increment(self):
        h = self.health()
        found, _ = h.observe({"speed": 1.0, "drop_frames": 5})
        self.assertEqual(found[0].key, "dropping")
        self.assertIn("5", found[0].message)

        # 増えていなければ黙る
        found, _ = h.observe({"speed": 1.0, "drop_frames": 5})
        self.assertEqual(found, [])

        # 増えたぶんだけ言う
        found, _ = h.observe({"speed": 1.0, "drop_frames": 8})
        self.assertIn("3", found[0].message)


class TestPrebufferSegments(unittest.TestCase):
    """秒数を「ライブ端から何セグメント手前か」に翻訳する。"""

    def count(self, seconds, body):
        from unittest.mock import patch

        from app.sources import segments_for_prebuffer

        with patch("app.sources._get", return_value=body):
            return segments_for_prebuffer("https://e.com/live.m3u8", seconds)

    MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts\n"

    def test_divides_by_target_duration(self):
        self.assertEqual(self.count(5, self.MEDIA), 3)   # 2秒セグメントなら3本で6秒
        self.assertEqual(self.count(10, self.MEDIA), 5)

    def test_zero_means_no_prebuffer(self):
        self.assertEqual(self.count(0, self.MEDIA), 0)

    def test_assumes_two_seconds_when_unknown(self):
        self.assertEqual(self.count(6, "#EXTM3U\n#EXTINF:2,\na.ts\n"), 3)

    def test_always_at_least_one_segment(self):
        self.assertEqual(self.count(1, "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"), 1)


class TestLivePlaylistDetection(unittest.TestCase):
    def detect(self, body, variant_body=None):
        from unittest.mock import patch

        from app.sources import _fetch_live_flag

        bodies = [body] + ([variant_body] if variant_body is not None else [])
        with patch("app.sources._get", side_effect=bodies):
            return _fetch_live_flag("https://e.com/x.m3u8")

    def test_endlist_means_vod(self):
        self.assertIs(self.detect("#EXTM3U\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n"), False)

    def test_playlist_type_vod_means_vod(self):
        self.assertIs(self.detect("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:4,\na.ts\n"), False)

    def test_no_endlist_means_live(self):
        self.assertIs(self.detect("#EXTM3U\n#EXTINF:4,\na.ts\n"), True)

    def test_master_playlist_follows_the_first_variant(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv1.m3u8\n"
        self.assertIs(self.detect(master, "#EXTM3U\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n"), False)

    def test_non_playlist_is_unknown(self):
        self.assertIsNone(self.detect("<html>nope</html>"))

    def test_unreachable_is_unknown(self):
        self.assertIsNone(self.detect(None))


class TestSingleJobGuard(unittest.IsolatedAsyncioTestCase):
    """同時に2本起動しないこと。

    start() はプロセス生成で await するため、チェックと状態設定のあいだに
    別のリクエストが割り込める。実際に1秒差で2本起動し、先に終わった側が
    共有状態を消して、もう1本が追跡不能になる事故が起きた。
    """

    def make_runner(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from app.config import Config
        from app.db import init_db
        from app.runner import JobRunner

        self._tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.data_dir = Path(self._tmp.name)
        cfg.log_dir = Path(self._tmp.name)
        cfg.media_dir = Path(self._tmp.name)
        conn = init_db(cfg.db_path)
        return JobRunner(conn, cfg), conn

    async def test_second_start_is_rejected_even_when_racing(self):
        import asyncio

        from unittest.mock import AsyncMock, patch

        from app.models import JobCreate
        from app.runner import JobBusy

        runner_obj, _ = self.make_runner()

        # プロセス生成で確実に譲る。ここが実際の割り込み地点
        async def slow_exec(*a, **kw):
            await asyncio.sleep(0.05)
            proc = AsyncMock()
            proc.pid = 4242
            proc.returncode = None
            proc.stdout = None
            proc.stderr = None
            proc.wait = AsyncMock(return_value=0)
            return proc

        spec = JobCreate(target_url="rtmp://e.com/live/key")
        with patch("asyncio.create_subprocess_exec", side_effect=slow_exec):
            results = await asyncio.gather(
                runner_obj.start(spec, "rtmp", url="rtmp://e.com/live/key"),
                runner_obj.start(spec, "rtmp", url="rtmp://e.com/live/key"),
                return_exceptions=True,
            )

        started = [r for r in results if isinstance(r, int)]
        rejected = [r for r in results if isinstance(r, JobBusy)]
        self.assertEqual(len(started), 1, "2本起動してしまった")
        self.assertEqual(len(rejected), 1)

    async def test_finalizing_another_job_does_not_clear_the_current_one(self):
        runner_obj, conn = self.make_runner()
        # いま追跡しているのは #99。無関係な #98 の終了で稼働中を落とさないこと
        runner_obj._active = True
        runner_obj._job_id = 99
        conn.execute(
            "INSERT INTO jobs (id,protocol,target_url,source_json,encode_json,"
            "duration_sec,status,started_at,resolved_command) "
            "VALUES (98,'rtmp','x','{}','{}',0,'running','now','cmd')"
        )
        conn.commit()

        runner_obj._finalize(98, 1)

        self.assertTrue(runner_obj._active, "別ジョブの終了で稼働中が落ちた")
        self.assertEqual(runner_obj._job_id, 99)
        row = conn.execute("SELECT status FROM jobs WHERE id=98").fetchone()
        self.assertEqual(row["status"], "failed")

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()


class TestReport(unittest.TestCase):
    """テスト後に振り返るためのJSON出力。"""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from app.db import init_db

        self._tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(Path(self._tmp.name) / "t.db")
        self.log = Path(self._tmp.name) / "job-1.log"
        self.log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
        self.conn.execute(
            """INSERT INTO jobs (id, protocol, target_url, target_label, source_json,
                                 encode_json, overlay_json, duration_sec, auto_restart, status,
                                 started_at, ended_at, resolved_command, exit_code,
                                 restart_count, last_error, log_path)
               VALUES (1,'srt','srt://h:9000?passphrase=●●●●●●','demo','{"type":"testsrc"}',
                       '{"width":1280}','{}',60,0,'stopped','2026-09-14T10:00:00+00:00',
                       '2026-09-14T10:01:00+00:00','ffmpeg ... ●●●●●●',0,0,NULL,?)""",
            (str(self.log),),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def add_samples(self, rates):
        for i, r in enumerate(rates):
            self.conn.execute(
                "INSERT INTO job_samples (job_id, at, rate, fps, bitrate_kbps, drop_frames,"
                " dup_frames, out_time_sec) VALUES (1,?,?,?,?,?,0,?)",
                (i * 5.0, r, 30.0, 2500.0, 0, i * 5.0),
            )
        self.conn.commit()

    def build(self, **kw):
        from app import report

        return report.build(self.conn, 1, **kw)

    def test_report_has_everything_needed_to_review(self):
        self.add_samples([1.0, 1.0, 0.99])
        r = self.build()
        for key in ("report_version", "tool", "job", "settings", "summary", "samples",
                    "events", "log"):
            self.assertIn(key, r)
        self.assertEqual(r["job"]["id"], 1)
        self.assertEqual(len(r["samples"]), 3)

    def test_secrets_stay_masked(self):
        from app import report

        self.add_samples([1.0])
        blob = report.dumps(self.build())      # ensure_ascii=False で出す
        self.assertIn("●●●●●●", blob)
        self.assertNotIn("passphrase=hunter", blob)

    def test_verdict_calls_out_sustained_slowness(self):
        self.add_samples([1.0, 0.7, 0.7, 0.7])
        self.assertIn("追いつけていない", self.build()["summary"]["verdict"])

    def test_verdict_calls_out_reconnects(self):
        self.conn.execute("UPDATE jobs SET restart_count=3 WHERE id=1")
        self.add_samples([1.0, 1.0])
        self.assertIn("再接続", self.build()["summary"]["verdict"])

    def test_verdict_calls_out_dropped_frames(self):
        self.conn.execute(
            "INSERT INTO job_samples (job_id, at, rate, fps, drop_frames) VALUES (1,5,1.0,30,12)"
        )
        self.conn.commit()
        self.assertIn("12 枚", self.build()["summary"]["verdict"])

    def test_healthy_run_says_so(self):
        self.add_samples([1.0, 1.0, 1.0])
        self.assertIn("安定して", self.build()["summary"]["verdict"])

    def test_events_are_included(self):
        self.conn.execute(
            "INSERT INTO job_events (job_id, at, kind, key, severity, message)"
            " VALUES (1, 12.0, 'diagnosis', 'slow_encode', 'warning', '遅い')"
        )
        self.conn.commit()
        self.add_samples([0.8])
        r = self.build()
        self.assertEqual(r["events"][0]["key"], "slow_encode")
        self.assertIn("slow_encode", r["summary"]["diagnoses"])

    def test_log_can_be_trimmed_or_omitted(self):
        self.add_samples([1.0])
        self.assertEqual(len(self.build(log_lines=10)["log"]["lines"]), 10)
        self.assertTrue(self.build(log_lines=10)["log"]["truncated"])
        # 0 は「ログ不要」。lines[-0:] が全件を返す罠を踏まないこと
        self.assertEqual(self.build(log_lines=0)["log"]["lines"], [])
        self.assertEqual(len(self.build(log_lines=None)["log"]["lines"]), 50)

    def test_missing_job_returns_none(self):
        from app import report

        self.assertIsNone(report.build(self.conn, 999))

    def test_filename_is_identifiable(self):
        from app import report

        name = report.filename({"id": 7, "started_at": "2026-09-14T10:00:00+00:00"})
        self.assertTrue(name.startswith("streamforge-job7-"))
        self.assertTrue(name.endswith(".json"))


class TestBenchmarkCommand(unittest.TestCase):
    """送出用コマンドを、どこにも送らない計測用へ書き換える。"""

    def test_output_is_replaced_with_null(self):
        from app.benchmark import to_null_output

        argv = ["ffmpeg", "-i", "a.mp4", "-c:v", "libx264", "-f", "flv", "rtmp://e.com/l/k"]
        out = to_null_output(argv)
        self.assertEqual(out[-2:], ["-f", "null"] if out[-1] == "null" else ["null", "-"])
        self.assertNotIn("rtmp://e.com/l/k", out)

    def test_duration_limit_is_dropped(self):
        from app.benchmark import to_null_output

        out = to_null_output(["ffmpeg", "-i", "a.mp4", "-t", "7200", "-f", "flv", "rtmp://x"])
        self.assertNotIn("-t", out)
        self.assertNotIn("7200", out)

    def test_input_format_flag_is_kept(self):
        from app.benchmark import to_null_output

        # 入力側の -f lavfi を出力の -f と取り違えないこと
        out = to_null_output(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc2", "-c:v", "libx264", "-f", "flv", "rtmp://x"]
        )
        self.assertIn("lavfi", out)
        self.assertNotIn("rtmp://x", out)

    def test_pacing_is_stripped(self):
        from app.benchmark import _strip_pacing

        out = _strip_pacing(["ffmpeg", "-re", "-readrate", "1", "-readrate_catchup", "1.5", "-i", "a"])
        # 実時間読みが残っていると必ず1.0倍になり、余裕が測れない
        self.assertEqual(out, ["ffmpeg", "-i", "a"])

    def test_verdict_thresholds(self):
        from app.benchmark import _verdict

        self.assertEqual(_verdict(7.4), "comfortable")
        self.assertEqual(_verdict(1.5), "tight")
        self.assertEqual(_verdict(0.8), "insufficient")


class TestEncoderFallback(unittest.TestCase):
    def test_videotoolbox_falls_back_when_unavailable(self):
        from unittest.mock import patch

        from app import main
        from app.capabilities import Capabilities

        caps = Capabilities(ffmpeg_path="ffmpeg", encoders={"libx264": True})
        with patch.dict(main.state, {"caps": caps}):
            enc, notes = main._apply_encoder_availability(
                EncodeSettings(video_encoder="h264_videotoolbox")
            )
        self.assertEqual(enc.video_encoder, "libx264")
        self.assertTrue(notes)

    def test_available_encoder_is_kept(self):
        from unittest.mock import patch

        from app import main
        from app.capabilities import Capabilities

        caps = Capabilities(
            ffmpeg_path="ffmpeg", encoders={"libx264": True, "h264_videotoolbox": True}
        )
        with patch.dict(main.state, {"caps": caps}):
            enc, notes = main._apply_encoder_availability(
                EncodeSettings(video_encoder="h264_videotoolbox")
            )
        self.assertEqual(enc.video_encoder, "h264_videotoolbox")
        self.assertEqual(notes, [])


class TestStateSnapshotDiagnoses(unittest.TestCase):
    """途中から繋いだクライアントにも、いま出ている指摘を渡す。

    指摘は発生時に1回しか流さないので、後から画面を開いた人には届かない。
    実際に稼働中のジョブへ途中から繋いだら、警告が何も見えなかった。
    """

    def runner(self):
        from app.runner import JobRunner

        from app.config import Config

        r = JobRunner.__new__(JobRunner)
        r.cfg = Config()
        r._ctx = {}
        r._job_id = 1
        r._active = True
        r._proc = None
        r._last_progress = {"speed": 0.8}
        r._log_buffer = ["line"]
        r._diagnoses = {"slow_encode": "追いついていません"}
        return r

    def test_active_diagnoses_are_included(self):
        snap = self.runner().snapshot()
        self.assertEqual(len(snap["diagnoses"]), 1)
        self.assertEqual(snap["diagnoses"][0]["key"], "slow_encode")
        self.assertFalse(snap["diagnoses"][0]["resolved"])

    def test_no_diagnoses_gives_empty_list(self):
        r = self.runner()
        r._diagnoses = {}
        self.assertEqual(r.snapshot()["diagnoses"], [])


class TestHeartbeat(unittest.TestCase):
    """順調なときFFmpegは何も言わないので、自分から定期報告する。"""

    def runner(self, duration=7200, restarts=0):
        from app.runner import JobRunner

        r = JobRunner.__new__(JobRunner)
        r._last_heartbeat = None
        r._heartbeats = 0
        r._ctx = {"restart_times": list(range(restarts)), "duration": duration}
        r.lines = []
        r._log = r.lines.append
        return r

    PROGRESS = {
        "out_time_sec": 3725, "fps": 30.0, "bitrate_kbps": 2500.4,
        "speed": 1.0, "drop_frames": 0,
    }

    def test_first_call_only_sets_the_baseline(self):
        from unittest.mock import patch

        r = self.runner()
        # 起動直後は基準を置くだけ。monotonic が 0.0 を返しても誤検知しないこと
        with patch("app.runner.time.monotonic", return_value=0.0):
            r._heartbeat(self.PROGRESS)
        self.assertEqual(r.lines, [])

    def test_emits_after_the_interval(self):
        from unittest.mock import patch

        from app.runner import FIRST_HEARTBEAT_SEC

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, FIRST_HEARTBEAT_SEC + 1]):
            r._heartbeat(self.PROGRESS)
            r._heartbeat(self.PROGRESS)
        self.assertEqual(len(r.lines), 1)
        line = r.lines[0]
        for token in ("送出中", "1:02:05", "fps 30.0", "bitrate 2500k", "レート 1.00x", "残り"):
            self.assertIn(token, line)

    def test_stays_quiet_before_the_interval(self):
        from unittest.mock import patch

        r = self.runner()
        with patch("app.runner.time.monotonic", side_effect=[0.0, 5.0, 10.0]):
            for _ in range(3):
                r._heartbeat(self.PROGRESS)
        self.assertEqual(r.lines, [])

    def test_reports_reconnects_when_they_happened(self):
        from unittest.mock import patch

        from app.runner import FIRST_HEARTBEAT_SEC

        r = self.runner(restarts=3)
        with patch("app.runner.time.monotonic", side_effect=[0.0, FIRST_HEARTBEAT_SEC + 1]):
            r._heartbeat(self.PROGRESS)
            r._heartbeat(self.PROGRESS)
        self.assertIn("再接続 3回", r.lines[0])

    def test_unlimited_job_has_no_remaining_time(self):
        from unittest.mock import patch

        from app.runner import FIRST_HEARTBEAT_SEC

        r = self.runner(duration=0)
        with patch("app.runner.time.monotonic", side_effect=[0.0, FIRST_HEARTBEAT_SEC + 1]):
            r._heartbeat(self.PROGRESS)
            r._heartbeat(self.PROGRESS)
        self.assertNotIn("残り", r.lines[0])


class TestLogThrottle(unittest.TestCase):
    def throttle(self):
        from app.runner import LogThrottle

        return LogThrottle()

    def test_repeated_lines_are_collapsed(self):
        t = self.throttle()
        shown = []
        now = 0.0
        for i in range(200):
            now += 0.02
            shown += t.feed(f"[mpegts @ 0x{i:x}] Invalid timestamps pts={i}, dts={i + 3}", now)
        # 200行が数行に収まること
        self.assertLess(len(shown), 10)
        self.assertTrue(any("省略しました" in ln for ln in shown))

    def test_different_lines_still_get_through(self):
        t = self.throttle()
        out = t.feed("first message", 0.0) + t.feed("totally different", 0.1)
        self.assertIn("first message", out)
        self.assertIn("totally different", out)

    def test_burst_of_distinct_lines_is_capped(self):
        from app.runner import MAX_EMIT_PER_SEC

        t = self.throttle()
        shown = []
        for i in range(200):
            shown += t.feed(f"unique message {i} {'x' * (i % 7)}", 0.5)
        self.assertLessEqual(len([s for s in shown if "streamforge" not in s]), MAX_EMIT_PER_SEC)

    def test_shape_ignores_addresses_and_numbers(self):
        from app.runner import log_shape

        self.assertEqual(
            log_shape("[mpegts @ 0xabc] pts=111, dts=222"),
            log_shape("[mpegts @ 0xdef] pts=333, dts=444"),
        )
        self.assertNotEqual(log_shape("Connection refused"), log_shape("pts=1"))


class TestVariantSelection(unittest.TestCase):
    MASTER = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=224x100\nv0.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=784x350\nv1.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1680x750\nv2.m3u8\n"
    )

    _DEFAULT = object()

    def pick(self, target, body=_DEFAULT):
        from unittest.mock import patch

        from app.sources import pick_variant

        served = self.MASTER if body is self._DEFAULT else body
        with patch("app.sources._get", return_value=served):
            return pick_variant("https://e.com/master.m3u8", target)

    def test_picks_smallest_variant_that_meets_the_target(self):
        # FFmpeg に任せると先頭（=最低画質）を掴み、拡大されて画質が崩れる
        url, note = self.pick(720)
        self.assertTrue(url.endswith("v2.m3u8"))
        self.assertIn("1680x750", note)

    def test_does_not_upscale_when_target_is_small(self):
        url, _ = self.pick(300)
        self.assertTrue(url.endswith("v1.m3u8"))

    def test_falls_back_to_largest_when_nothing_reaches_target(self):
        url, _ = self.pick(2160)
        self.assertTrue(url.endswith("v2.m3u8"))

    def test_media_playlist_is_left_alone(self):
        url, note = self.pick(720, "#EXTM3U\n#EXTINF:4,\na.ts\n")
        self.assertEqual(url, "https://e.com/master.m3u8")
        self.assertEqual(note, "")

    def test_unreachable_master_is_left_alone(self):
        url, note = self.pick(720, None)
        self.assertEqual(url, "https://e.com/master.m3u8")
        self.assertEqual(note, "")


class TestSeamHandling(unittest.TestCase):
    def resolve(self, seam="auto", duration=3.7, w=1280, h=720, fps=30):
        from unittest.mock import patch

        from app.sources import resolve_seam

        src = SourceSpec(type="file", path="a.mp4", loop=True, seam=seam)
        with patch("app.sources.probe_duration", return_value=duration):
            return resolve_seam(src, "/tmp/a.mp4", w, h, fps)

    def test_short_clip_is_buffered(self):
        # -stream_loop は継ぎ目で約200ms出力が止まり、映像と音声が同時に乱れる
        use, frames, note = self.resolve(duration=3.7)
        self.assertTrue(use)
        self.assertEqual(frames, int(3.7 * 30) + 1)
        self.assertIn("途切れない", note)

    def test_long_clip_falls_back_to_reload(self):
        use, frames, note = self.resolve(duration=3600)
        self.assertFalse(use)
        self.assertEqual(frames, 0)
        self.assertIn("素材が長いため", note)

    def test_explicit_reload_is_respected(self):
        self.assertFalse(self.resolve(seam="reload")[0])

    def test_explicit_buffer_ignores_the_budget(self):
        self.assertTrue(self.resolve(seam="buffer", duration=3600)[0])

    def test_unknown_duration_falls_back(self):
        use, _, note = self.resolve(duration=None)
        self.assertFalse(use)
        self.assertIn("長さを取得できない", note)


class TestSeamInCommand(unittest.TestCase):
    def build(self, seam_frames):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.mp4").write_bytes(b"x")
            return build_command(
                ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://e.com/l/k",
                source=SourceSpec(type="file", path="a.mp4", loop=True),
                encode=EncodeSettings(fps=30), duration_sec=None,
                media_dir=d, seam_frames=seam_frames,
            )

    def test_buffered_loop_drops_stream_loop(self):
        argv = self.build(112)
        self.assertNotIn("-stream_loop", argv)
        vf = argv[argv.index("-filter_complex") + 1]
        self.assertIn("loop=loop=-1:size=112", vf)
        self.assertIn("aloop=loop=-1", argv[argv.index("-af") + 1])

    def test_reload_loop_keeps_stream_loop(self):
        argv = self.build(0)
        self.assertIn("-stream_loop", argv)
        self.assertNotIn("loop=loop=-1", argv[argv.index("-filter_complex") + 1])


class TestWebrtcDelivery(unittest.TestCase):
    """SRT/RTMP で送るが、配信先が WebRTC で配信する場合（Ceeblue 等）。"""

    def adapt(self, protocol, flag, enc=None):
        from app.command import adapt_encode

        return adapt_encode(enc or EncodeSettings(), protocol, webrtc_delivery=flag)

    def test_video_is_made_webrtc_safe(self):
        # Bフレーム入りの Main/High はブラウザがデコードできず映像だけ真っ暗になる
        enc, changes = self.adapt("srt", True)
        self.assertEqual(enc.bframes, 0)
        self.assertEqual(enc.profile, "baseline")
        self.assertTrue(any("真っ暗" in c for c in changes))

    def test_audio_is_left_alone_for_srt(self):
        # SRT は MPEG-TS で AAC を運び、Opus への変換は配信先が行う。
        # ここで Opus にすると逆に運べなくなる
        enc, _ = self.adapt("srt", True)
        self.assertEqual(enc.audio_codec, "aac")

    def test_audio_becomes_opus_only_for_whip(self):
        enc, _ = self.adapt("whip", False)
        self.assertEqual(enc.audio_codec, "opus")

    def test_flag_off_changes_nothing(self):
        enc, changes = self.adapt("srt", False)
        self.assertEqual(changes, [])
        self.assertEqual(enc.profile, "high")
        self.assertEqual(enc.bframes, 2)

    def test_reaches_the_command(self):
        argv = build_command(
            ffmpeg_path="ffmpeg", protocol="srt", target_url="srt://h:9000",
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=None,
            webrtc_delivery=True,
        )
        self.assertEqual(argv[argv.index("-bf") + 1], "0")
        self.assertEqual(argv[argv.index("-profile:v") + 1], "baseline")
        self.assertIn("aac", argv)


class TestWebrtcTemplate(unittest.TestCase):
    def test_builtin_template_exists_and_is_safe(self):
        from app.templates import BUILTINS

        name, enc = next((n, e) for n, e in BUILTINS if "WebRTC" in n)
        self.assertEqual(enc.bframes, 0)
        self.assertEqual(enc.profile, "baseline")


class TestVideoToolboxClosedCaptions(unittest.TestCase):
    def test_videotoolbox_disables_a53cc(self):
        from app.command import build_encode_args

        args = build_encode_args(EncodeSettings(video_encoder="h264_videotoolbox"), SourceSpec())
        self.assertEqual(args[args.index("-a53cc") + 1], "0")

    def test_libx264_keeps_default_behaviour(self):
        from app.command import build_encode_args

        args = build_encode_args(EncodeSettings(video_encoder="libx264"), SourceSpec())
        self.assertNotIn("-a53cc", args)


class TestBackoff(unittest.TestCase):
    def test_backoff_grows_then_plateaus(self):
        from app.runner import BACKOFF_SEC

        self.assertEqual(BACKOFF_SEC[0], 2)
        self.assertTrue(all(a < b for a, b in zip(BACKOFF_SEC, BACKOFF_SEC[1:])))
        self.assertLessEqual(BACKOFF_SEC[-1], 60)


class TestWhip(unittest.TestCase):
    def build(self, **kwargs):
        params = dict(
            ffmpeg_path="ffmpeg", protocol="whip",
            target_url="https://live.example.com/whip/abc",
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=None,
        )
        params.update(kwargs)
        return build_command(**params)

    def test_https_resolves_to_whip_and_is_supported(self):
        r = resolve("https://live.example.com/whip/abc")
        self.assertEqual(r.protocol, "whip")
        self.assertTrue(r.supported)

    def test_non_whip_path_is_warned(self):
        r = resolve("https://live.example.com/publish/abc")
        self.assertTrue(any("/whip" in w for w in r.warnings))

    def test_plain_http_is_warned(self):
        r = resolve("http://live.example.com/whip/abc")
        self.assertTrue(any("DTLS" in w for w in r.warnings))

    def test_uses_whip_muxer(self):
        argv = self.build()
        self.assertEqual(argv[-3:], ["-f", "whip", "https://live.example.com/whip/abc"])

    def test_bearer_token_goes_to_authorization_not_url(self):
        argv = self.build(bearer_token="tok-abc")
        self.assertEqual(argv[argv.index("-authorization") + 1], "tok-abc")
        self.assertNotIn("tok-abc", argv[-1])

    def test_no_token_means_no_authorization_flag(self):
        self.assertNotIn("-authorization", self.build())

    def test_handshake_timeout_is_extended(self):
        self.assertIn("-handshake_timeout", self.build())


class TestWhipAdaptation(unittest.TestCase):
    def adapt(self, enc=None, protocol="whip"):
        from app.command import adapt_encode

        return adapt_encode(enc or EncodeSettings(), protocol)

    def test_audio_is_switched_to_opus(self):
        enc, changes = self.adapt()
        self.assertEqual(enc.audio_codec, "opus")
        self.assertTrue(any("Opus" in c for c in changes))

    def test_bframes_are_disabled(self):
        enc, changes = self.adapt(EncodeSettings(bframes=3))
        self.assertEqual(enc.bframes, 0)
        self.assertTrue(any("Bフレーム" in c for c in changes))

    def test_long_gop_is_shortened(self):
        enc, _ = self.adapt(EncodeSettings(gop_sec=5.0))
        self.assertEqual(enc.gop_sec, 2.0)

    def test_short_gop_is_left_alone(self):
        enc, _ = self.adapt(EncodeSettings(gop_sec=1.0))
        self.assertEqual(enc.gop_sec, 1.0)

    def test_profile_is_lowered_to_baseline(self):
        # WebRTC のブラウザは constrained baseline でネゴシエートする
        enc, _ = self.adapt(EncodeSettings(profile="high"))
        self.assertEqual(enc.profile, "baseline")

    def test_already_compliant_settings_report_no_changes(self):
        compliant = EncodeSettings(
            audio_codec="opus", bframes=0, gop_sec=2.0, profile="baseline",
            samplerate=48000, channels=2,
        )
        enc, changes = self.adapt(compliant)
        self.assertEqual(changes, [])

    def test_other_protocols_are_untouched(self):
        enc, changes = self.adapt(protocol="rtmp")
        self.assertEqual(enc.audio_codec, "aac")
        self.assertEqual(changes, [])

    def test_adaptation_reaches_the_command(self):
        argv = build_command(
            ffmpeg_path="ffmpeg", protocol="whip", target_url="https://e.com/whip",
            source=SourceSpec(), encode=EncodeSettings(bframes=2), duration_sec=None,
        )
        self.assertEqual(argv[argv.index("-bf") + 1], "0")
        self.assertIn("libopus", argv)

    def test_aac_is_kept_for_rtmp(self):
        argv = build_command(
            ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://e.com/l/k",
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=None,
        )
        self.assertIn("aac", argv)
        self.assertNotIn("libopus", argv)


class TestVideoBasedRate(unittest.TestCase):
    """映像が止まったことを、計測が見逃さないか。

    FFmpeg の out_time は音声・映像のうち**進んでいる方**を指す。実機では映像の
    フレーム生成が128秒間止まっているあいだ、out_time も rate も 1.00x のままで、
    警告が一度も出なかった。ここはその再発を止めるための取り決め。
    """

    def health(self, samples: list[dict]):
        from app.errors import ProgressHealth
        h = ProgressHealth()
        found: list = []
        for s in samples:
            f, _ = h.observe(s)
            found += f
        return found

    def test_stopped_video_is_reported_even_though_audio_flows(self):
        # 音声が流れているので out_time 基準では 1.00x。映像基準なら 0.00x
        samples = [
            {"rate": 1.0, "video_rate": 0.0, "frame": 30425, "out_time_sec": 500 + i}
            for i in range(12)
        ]
        keys = [d.key for d in self.health(samples)]
        self.assertIn("slow_encode", keys)

    def test_out_time_rate_alone_would_have_missed_it(self):
        # video_rate を渡さない＝従来の見え方。何も出ないことを明示しておく
        samples = [{"rate": 1.0, "out_time_sec": 500 + i} for i in range(12)]
        self.assertEqual([d.key for d in self.health(samples)], [])

    def test_encoder_lead_alone_never_warns(self):
        # av_skew_sec は音声エンコーダの到達点との差で、送出される中身のずれでは
        # ない（-max_interleave_delta 0 が映像を待たせる）。重い条件では平常時でも
        # 100秒を超えるので、ここで鳴らすと誤報になる
        samples = [
            {"rate": 1.0, "video_rate": 1.0, "av_skew_sec": 126.0, "out_time_sec": 500 + i}
            for i in range(12)
        ]
        self.assertEqual([d.key for d in self.health(samples)], [])

    def test_video_rate_is_preferred_over_out_time(self):
        from app.errors import pick_rate
        self.assertEqual(pick_rate({"rate": 1.0, "video_rate": 0.0}), (0.0, True))
        self.assertEqual(pick_rate({"rate": 0.8}), (0.8, False))

    def test_report_verdict_says_video_stopped(self):
        from app.report import summarize
        samples = [
            {"at": float(i), "rate": 1.0, "video_rate": 0.0 if i > 5 else 1.0,
             "av_skew_sec": 0.0, "fps": 48.0, "bitrate_kbps": 8000.0,
             "drop_frames": 0, "dup_frames": 0, "out_time_sec": float(i)}
            for i in range(20)
        ]
        summary = summarize(samples, [], {"restart_count": 0})
        self.assertEqual(summary["rate"]["basis"], "video")
        self.assertIn("映像が出ていない", summary["verdict"])

    def test_encoder_lead_is_recorded_but_not_a_verdict(self):
        from app.report import summarize
        samples = [
            {"at": float(i), "rate": 1.0, "video_rate": 1.0, "av_skew_sec": 126.0,
             "fps": 60.0, "bitrate_kbps": 8000.0, "drop_frames": 0, "dup_frames": 0,
             "out_time_sec": float(i)}
            for i in range(20)
        ]
        summary = summarize(samples, [], {"restart_count": 0})
        self.assertEqual(summary["encoder_lead_sec"]["max"], 126.0)
        self.assertIn("安定して送出できました", summary["verdict"])


class TestSelfPreviewCommand(unittest.TestCase):
    """セルフプレビュー（送出中の絵を送出側で見る）のコマンド生成。

    ここで守っているのは、検討中に実際に踏んだ罠ばかり。どれも「動くけれど
    どこかで詰まる」形で出るので、テストで固定しておく。
    """

    def build(self, **kwargs):
        from app.command import SnapshotSpec
        from app.models import OverlaySettings

        params = dict(
            ffmpeg_path="ffmpeg", protocol="rtmp", target_url="rtmp://example.com/live/key",
            source=SourceSpec(), encode=EncodeSettings(), duration_sec=60,
            overlay=OverlaySettings(), font_path="/tmp/f.ttf",
            snapshot=SnapshotSpec(path="/tmp/snap/current.jpg", width=480, fps=1.0),
        )
        params.update(kwargs)
        return build_command(**params)

    def graph(self, argv):
        return argv[argv.index("-filter_complex") + 1]

    def test_without_snapshot_there_is_no_branch(self):
        argv = self.build(snapshot=None)
        self.assertNotIn("split=2", self.graph(argv))
        self.assertNotIn("image2", argv)
        self.assertNotIn("-y", argv)

    def test_preview_output_comes_after_the_send_output(self):
        # -progress が報告するのは「最初の出力」だけ。プレビューを先に置くと
        # 監視が毎秒1枚の枝を見てしまい、video_rate が 1/30 に見えて
        # 「映像が出ていません」の誤報になる（実際に出た）
        argv = self.build()
        self.assertLess(
            argv.index("rtmp://example.com/live/key"), argv.index("image2")
        )
        self.assertEqual(argv[-1], "/tmp/snap/current.jpg")

    def test_measurement_commands_drop_the_pacing(self):
        # 実時間に絞ったまま測ると必ず 1.0 倍になり、「この設定で落ちるか」に
        # 答えられない。`-re` から realtime フィルタへ移したときに、計測側の
        # 絞り外し（-re だけを見ていた）が効かなくなっていた
        argv = self.build(snapshot=None, pacing=False)
        self.assertNotIn("realtime", argv[argv.index("-filter_complex") + 1])
        self.assertNotIn("-re", argv)
        # 通常の送出では今までどおり絞る
        self.assertIn("realtime", self.build(snapshot=None)[
            self.build(snapshot=None).index("-filter_complex") + 1])

    def test_measurement_drops_pacing_for_passthrough_too(self):
        # -c copy はフィルタを通らないので入力側の -re が効く。そこも外す
        argv = self.build(
            snapshot=None, pacing=False, encode=EncodeSettings(mode="passthrough")
        )
        self.assertNotIn("-re", argv)

    def test_measuring_a_preview_command_is_refused(self):
        # 末尾を差し替える計測用の書き換えに通すと、送出側が生き残って
        # 「計測のつもりで本当に送る」ことになる。黙って間違えるより落とす
        from app.benchmark import to_null_output

        with self.assertRaises(ValueError):
            to_null_output(self.build())
        # プレビュー無しなら従来どおり通る
        self.assertEqual(to_null_output(self.build(snapshot=None))[-3:], ["-f", "null", "-"])

    def test_branch_is_taken_after_the_overlay(self):
        # プレビューに写るのは「焼き込み済みのフレームそのもの」でなければ
        # 意味がない。split がオーバーレイより前だと、時計の無い絵を見ることになる
        graph = self.graph(self.build())
        self.assertLess(graph.index("drawtext"), graph.index("split=2"))
        self.assertLess(graph.rindex("drawtext"), graph.index("split=2"))

    def test_snapshot_output_has_its_own_timeout(self):
        # -t は出力ごとの指定。送出側にだけ付けると、プレビュー枝が終わらず
        # プロセスが一生終わらない（lavfi 入力で実際に止まらなくなった）
        argv = self.build(duration_sec=60)
        tail = argv[argv.index("image2") - 12:]
        self.assertIn("-t", tail)
        self.assertEqual(argv.count("-t"), 2)   # プレビューと送出の両方

    def test_unlimited_duration_puts_no_timeout_anywhere(self):
        self.assertNotIn("-t", self.build(duration_sec=0))

    def test_overwrite_prompt_is_disabled(self):
        # image2 は既存ファイルがあると Overwrite? [y/N] を聞いて止まる。
        # -nostdin と組み合わさると即死する
        argv = self.build()
        self.assertIn("-y", argv)
        self.assertLess(argv.index("-y"), argv.index("-i"))   # グローバル指定

    def test_torn_reads_are_prevented(self):
        # -update 1 は同じファイルを切り詰めて書き直す。rename に変えないと
        # 読み手が書きかけの JPEG を掴む
        argv = self.build()
        self.assertEqual(argv[argv.index("-atomic_writing") + 1], "1")
        self.assertEqual(argv[argv.index("-update") + 1], "1")

    def test_audio_filters_belong_to_the_send_output(self):
        # -af は「次の出力」に掛かる指定。送出が先頭の出力なので、-af は
        # そこより前に無ければならない（プレビュー側に吸われたら音声に掛からない）
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as d:
            _Path(d, "a.mp4").write_bytes(b"x")
            argv = self.build(
                source=SourceSpec(type="file", path="a.mp4", loop=True), media_dir=d
            )
            self.assertIn("-af", argv)
            self.assertLess(argv.index("-af"), argv.index("-c:v"))
            self.assertLess(argv.index("-af"), argv.index("image2"))

    def test_passthrough_gets_no_preview(self):
        # -c copy はフィルタを通らないので、分岐する場所が無い
        argv = self.build(encode=EncodeSettings(mode="passthrough"))
        self.assertNotIn("image2", argv)
        self.assertNotIn("-y", argv)

    def test_snapshot_is_scaled_and_thinned(self):
        graph = self.graph(self.build())
        self.assertIn("fps=1,scale=480:-2", graph)

    def test_video_is_mapped_from_the_graph(self):
        argv = self.build()
        self.assertIn("[vout]", argv)
        self.assertNotIn("0:v", argv)


class TestSnapshotFreshness(unittest.TestCase):
    """静止画の更新が止まったら言う。数値とは独立した2本目の物差し。"""

    def health(self):
        from app.errors import ProgressHealth

        return ProgressHealth()

    def feed(self, health, *, age, video_rate=1.0, samples=30):
        found = []
        for i in range(samples):
            f, r = health.observe(
                {"frame": (i + 1) * 30, "out_time_sec": float(i + 1),
                 "video_rate": video_rate, "snapshot_age_sec": age}
            )
            found += f
        return found

    def test_fresh_snapshot_says_nothing(self):
        found = self.feed(self.health(), age=1.0)
        self.assertEqual([f for f in found if f.key == "snapshot_stale"], [])

    def test_stale_snapshot_is_reported_once(self):
        found = self.feed(self.health(), age=40.0)
        stale = [f for f in found if f.key == "snapshot_stale"]
        self.assertEqual(len(stale), 1)
        self.assertIn("更新されていません", stale[0].message)

    def test_stopped_video_is_left_to_slow_encode(self):
        # 映像が止まっていること自体は slow_encode が言う。ここで重ねて言わない
        found = self.feed(self.health(), age=40.0, video_rate=0.0)
        self.assertEqual([f for f in found if f.key == "snapshot_stale"], [])

    def test_recovery_is_announced(self):
        health = self.health()
        self.feed(health, age=40.0)
        _, resolved = health.observe(
            {"frame": 9999, "out_time_sec": 999.0, "video_rate": 1.0, "snapshot_age_sec": 1.0}
        )
        self.assertIn("snapshot_stale", resolved)

    def test_disabled_preview_is_never_judged(self):
        # キーが無い＝プレビューを出していない。存在しないものを警告しない
        health = self.health()
        found = []
        for i in range(30):
            f, _ = health.observe(
                {"frame": (i + 1) * 30, "out_time_sec": float(i + 1), "video_rate": 1.0}
            )
            found += f
        self.assertEqual([f for f in found if f.key == "snapshot_stale"], [])


if __name__ == "__main__":
    unittest.main()
