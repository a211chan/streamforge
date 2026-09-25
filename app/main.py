"""StreamForge API サーバー。

P0: testsrc → RTMP 1本を、ブラウザから開始・停止できる
P1: URLを貼るだけでプロトコルを判定し、SRT も送出できる。送出先を保存・再利用する
P2: エンコード設定は送出時に自動でテンプレート化され、次回から選べる
P3: 映像に時計とエンコード設定を焼き込む
P4: ファイル／外部ライブソース、自動再接続、エラー翻訳
P6: WebRTC(WHIP) 送出
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import benchmark, capabilities, clock, fonts, probe, protocol, report, runner, sources
from .command import SnapshotSpec, adapt_encode, build_command, to_shell
from .config import load_config
from .db import init_db
from .models import (
    EncodeSettings,
    JobCreate,
    OverlaySettings,
    PreviewRequest,
    SourceSpec,
    TargetSave,
    TemplatePatch,
)
from .secrets import SecretBox, redact
from .targets import TargetStore
from .templates import TemplateStore

STATIC_DIR = Path(__file__).parent / "static"

cfg = load_config()
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    caps = capabilities.detect(cfg.ffmpeg_path)
    box = SecretBox(cfg.data_dir)
    job_runner = runner.JobRunner(conn, cfg)
    orphaned = job_runner.reconcile_on_startup()
    template_store = TemplateStore(conn)
    template_store.seed_builtins()
    font = fonts.resolve(cfg.font_path)

    state.update(
        conn=conn,
        caps=caps,
        runner=job_runner,
        targets=TargetStore(conn, box),
        templates=template_store,
        font=font,
    )

    log = lambda msg: print(f"[streamforge] {msg}", flush=True)
    log(f"ffmpeg: {caps.ffmpeg_path} ({caps.version or 'not found'})")
    log(f"db: {cfg.db_path}")
    log(f"送出可能: {', '.join(caps.as_dict()['ready_protocols']) or 'なし'}")
    log(f"オーバーレイ用フォント: {font.path if font else '見つかりません（オーバーレイ無効）'}")
    if orphaned:
        log(f"前回の running ジョブを整理しました: {orphaned}")
    if not caps.available:
        log(f"警告: {caps.error}")

    try:
        yield
    finally:
        await job_runner.shutdown()
        conn.close()


app = FastAPI(title="StreamForge", lifespan=lifespan)


def get_runner() -> runner.JobRunner:
    return state["runner"]


def get_targets() -> TargetStore:
    return state["targets"]


def get_templates() -> TemplateStore:
    return state["templates"]


# --------------------------------------------------------------------- 画面

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/jobs/{job_id}")
async def job_page(job_id: int) -> FileResponse:
    """稼働中・終了済みのジョブを、それだけ見る画面。

    同じ画面（index.html）を返し、パスを見て画面側が監視モードに切り替える。
    人に配るURLではなく、同じ端末の別タブ・ブックマーク用なので認証は足さない。
    サーバーは 127.0.0.1 に閉じたままにしておくこと。
    """
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------ システム

@app.get("/api/system/capabilities")
async def system_capabilities() -> dict:
    caps = state["caps"].as_dict()
    caps["phase"] = "P6"
    caps["supported_protocols"] = sorted(protocol.SUPPORTED)
    font = state.get("font")
    caps["font"] = font.as_dict() if font else None
    caps["overlay_ready"] = bool(caps["overlay_ready"] and font)
    return caps


@app.get("/api/system/clock")
async def system_clock() -> dict:
    """壁時計オーバーレイの前提になる時刻同期の状態。

    時計が合っていなければ、測った絶対遅延もそのままずれる。
    """
    return await asyncio.to_thread(clock.cached_status, cfg.ntp_server)


# ------------------------------------------------------------------ 送出先

class ResolveRequest(BaseModel):
    url: str


def _apply_runtime_support(res) -> None:
    """判定できても、このビルドの FFmpeg が扱えなければ送出できない。

    WHIP はプロトコルではなく muxer として実装されているので、確認先が違う。
    ここを能力検出に紐づけてあるので、対応ビルドに差し替えれば自動で解禁される。
    """
    if not res.supported:
        return
    caps = state["caps"]

    if res.protocol in ("rtmp", "srt") and not caps.protocols.get(res.protocol):
        res.supported = False
        res.reason = f"FFmpeg が {res.protocol.upper()} プロトコルを持っていません"
    elif res.protocol == "whip" and not caps.muxers.get("whip"):
        res.supported = False
        res.reason = (
            "FFmpeg に WHIP muxer がありません。DTLSを張るため openssl 等を有効にした"
            "ビルドが必要です（brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-openssl@3）"
        )


@app.post("/api/targets/resolve")
async def resolve_target(req: ResolveRequest) -> dict:
    """URL を貼った時点で呼ばれる。判定結果・分解したパラメータ・警告を返す。"""
    res, known = get_targets().resolve_with_learning(req.url)
    _apply_runtime_support(res)
    payload = res.as_dict()
    payload["known_target"] = known
    return payload


@app.get("/api/targets")
async def list_targets(limit: int = 30) -> dict:
    return {"targets": get_targets().list(limit)}


@app.post("/api/targets", status_code=201)
async def save_target(req: TargetSave) -> dict:
    """送出先を保存する。protocol を明示した場合は手動上書きとして記録する。"""
    store = get_targets()
    res, _ = store.resolve_with_learning(req.url)

    detected_by = "auto"
    if req.protocol and req.protocol != res.protocol:
        if req.protocol not in ("rtmp", "srt", "whip"):
            raise HTTPException(status_code=400, detail=f"未知のプロトコル: {req.protocol}")
        res.protocol = req.protocol
        res.supported = req.protocol in protocol.SUPPORTED
        res.reason = f"手動で {req.protocol.upper()} に変更されました"
        detected_by = "manual"

    if res.protocol is None:
        raise HTTPException(status_code=400, detail=res.reason)

    return store.save(
        req.url, res, label=req.label, detected_by=detected_by,
        bearer_token=req.bearer_token or None,
    )


@app.delete("/api/targets/{target_id}")
async def delete_target(target_id: int) -> dict:
    if not get_targets().delete(target_id):
        raise HTTPException(status_code=404, detail="送出先が見つかりません")
    return {"deleted": True, "target_id": target_id}


class TestRequest(BaseModel):
    url: str = ""
    target_id: int | None = None
    bearer_token: str = ""


@app.post("/api/targets/test")
async def test_target(req: TestRequest) -> dict:
    """最小構成で数秒だけ送り、ハンドシェイクの成否を返す（F-1 疎通確認）。"""
    if get_runner().is_running():
        raise HTTPException(status_code=409, detail="送出中は疎通確認できません。先に停止してください")

    url, res, token = _resolve_target_input(req.url, req.target_id, req.bearer_token)
    return await probe.test_connection(
        cfg.ffmpeg_path,
        res.protocol,
        url,
        secrets=list(res.secrets.values()),
        display_url=get_targets().display_url_for(url, res),
        bearer_token=token,
    )


def _resolve_target_input(url: str, target_id: int | None, bearer_token: str = ""):
    """URL か target_id から、送出に使う原文URL・判定結果・トークンを得る。

    送出できない場合はここで 400 を返すので、呼び出し側は結果を信用してよい。
    """
    store = get_targets()
    token = bearer_token
    if target_id is not None:
        stored_url = store.get_url(target_id)
        if stored_url is None:
            raise HTTPException(status_code=404, detail="送出先が見つかりません")
        url = stored_url
        # 保存済みトークンは、明示的に上書きされない限りそのまま使う
        token = bearer_token or store.get_token(target_id)
    if not (url or "").strip():
        raise HTTPException(status_code=400, detail="配信先URLを入力してください")

    res, _ = store.resolve_with_learning(url)
    _apply_runtime_support(res)
    if not res.supported or res.protocol is None:
        raise HTTPException(status_code=400, detail=res.reason)
    if token:
        res.secrets["bearer_token"] = token
    return url, res, token


# -------------------------------------------------------------------- ソース

@app.get("/api/sources")
async def list_sources() -> dict:
    """使える映像ソース。ファイルは media_dir を毎回読み直す。"""
    return {
        "media_dir": str(cfg.media_dir),
        "files": sources.list_media(cfg.media_dir),
        "catalog": sources.catalog(),
    }


@app.post("/api/sources/upload", status_code=201)
async def upload_source(file: UploadFile = File(...)) -> dict:
    try:
        dest = sources.safe_upload_path(cfg.media_dir, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > sources.MAX_UPLOAD_BYTES:
                    raise ValueError("ファイルが大きすぎます（上限2GB）")
                out.write(chunk)
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    return {"name": dest.name, "size_bytes": written}


@app.delete("/api/sources/{name}")
async def delete_source(name: str) -> dict:
    try:
        target = sources.safe_upload_path(cfg.media_dir, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.exists():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    target.unlink()
    return {"deleted": True, "name": target.name}


def _check_source(source: SourceSpec) -> None:
    """送出前にソースの前提を確かめる。"""
    if source.type == "file":
        from .command import resolve_media_path

        try:
            resolve_media_path(source.path, str(cfg.media_dir))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif source.type == "live_url":
        if not source.url.strip():
            raise HTTPException(status_code=400, detail="外部ライブソースのURLが空です")
        if not source.url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="外部ライブソースは http(s) のみ対応しています"
                "（自動再接続がHTTP固有の機能のため）",
            )


# ------------------------------------------------------------ テンプレート

@app.get("/api/templates")
async def list_templates() -> dict:
    return {"templates": get_templates().list()}


@app.patch("/api/templates/{template_id}")
async def patch_template(template_id: int, req: TemplatePatch) -> dict:
    try:
        updated = get_templates().patch(template_id, name=req.name, is_pinned=req.is_pinned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="テンプレートが見つかりません")
    return updated


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: int) -> dict:
    ok, reason = get_templates().delete(template_id)
    if not ok:
        raise HTTPException(status_code=404 if "見つかりません" in reason else 400, detail=reason)
    return {"deleted": True, "template_id": template_id}


def _apply_encoder_availability(encode: EncodeSettings) -> tuple[EncodeSettings, list[str]]:
    """使えないエンコーダが指定されていたら、動くものに読み替える。

    VideoToolbox は macOS 専用。テンプレートをそのまま Linux へ持っていくと
    起動すらしないので、能力検出の結果に合わせて落とす（憲章「Macに閉じない」）。
    """
    caps = state["caps"]
    if encode.video_encoder == "libx264" or caps.encoders.get(encode.video_encoder):
        return encode, []
    return (
        encode.model_copy(update={"video_encoder": "libx264"}),
        [f"{encode.video_encoder} がこのビルドに無いため libx264 に切り替えました"],
    )


def _resolve_encode(req: JobCreate) -> EncodeSettings:
    """テンプレート指定があればそれを、無ければリクエストの設定を使う。"""
    if req.template_id is None:
        return req.encode
    enc = get_templates().settings_of(req.template_id)
    if enc is None:
        raise HTTPException(status_code=404, detail="テンプレートが見つかりません")
    return enc


def _prepare_source(source: SourceSpec, encode: EncodeSettings):
    """送出前に、ネットワークやファイルを見て決まることをまとめて決める。

    コマンド生成側を純関数に保つため、外を触る判断はここに閉じ込める。
    戻り値は (調整後のソース, 継ぎ目バッファのフレーム数, 説明の列)。
    """
    notes: list[str] = []
    prepared, live_notes = sources.prepare_live_source(source, encode.height)
    notes += live_notes

    seam_frames = 0
    if prepared.type == "file":
        try:
            from .command import resolve_media_path

            path = resolve_media_path(prepared.path, str(cfg.media_dir))
        except ValueError:
            return prepared, 0, notes
        use_buffer, seam_frames, seam_note = sources.resolve_seam(
            prepared, path, encode.width, encode.height, encode.fps
        )
        if not use_buffer:
            seam_frames = 0
        if seam_note:
            notes.append(seam_note)

    return prepared, seam_frames, notes


def _snapshot_spec() -> SnapshotSpec | None:
    """セルフプレビューの指定。設定で切られていれば None。

    passthrough のときに落とすのは `build_command()` 側の仕事なので、ここでは見ない。
    """
    if not cfg.snapshot_enabled:
        return None
    return SnapshotSpec(
        path=str(cfg.snapshot_path), width=cfg.snapshot_width, fps=cfg.snapshot_fps
    )


def _font_path() -> str:
    font = state.get("font")
    return font.path if font else ""


def _check_overlay(encode: EncodeSettings, overlay: OverlaySettings) -> None:
    """焼き込めない条件を、送出前に理由付きで止める。"""
    if not overlay.enabled():
        return
    if encode.mode == "passthrough":
        raise HTTPException(
            status_code=400,
            detail="Passthrough は再エンコードしないため、オーバーレイを焼き込めません。"
            "オーバーレイを切るか、別のテンプレートを選んでください",
        )
    if not _font_path():
        raise HTTPException(
            status_code=503,
            detail="オーバーレイ用のフォントが見つかりません。config.toml の overlay.font_path "
            "を設定するか、assets/fonts に等幅フォントを置いてください",
        )
    if not state["caps"].filters.get("drawtext"):
        raise HTTPException(
            status_code=503,
            detail="FFmpeg に drawtext フィルタがありません（libfreetype 有効のビルドが必要です）",
        )


def _check_source_compatibility(encode: EncodeSettings, source: SourceSpec) -> None:
    if encode.mode == "passthrough" and source.type == "testsrc":
        raise HTTPException(
            status_code=400,
            detail="Passthrough は入力をそのまま流すため、テストパターンには使えません。"
            "ファイルまたは外部ライブソースを選んでください",
        )


# -------------------------------------------------------------------- ジョブ

@app.get("/api/defaults")
async def defaults() -> dict:
    """新規ジョブ画面の初期値。前回の選択をそのまま復元する。

    連続してテストするときに「開始」を押すだけで済むようにするための機能。
    """
    conn = state["conn"]
    row = conn.execute(
        "SELECT target_id, target_url, source_json, encode_json, overlay_json FROM jobs "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    last_template = get_templates().last_used()
    if row:
        encode = json.loads(row["encode_json"])
        source = json.loads(row["source_json"])
    else:
        encode = EncodeSettings().model_dump()
        source = SourceSpec().model_dump()

    return {
        "target_id": row["target_id"] if row else None,
        "target_display_url": row["target_url"] if row else "",
        "template_id": last_template["id"] if last_template else None,
        "encode": encode,
        "source": source,
        "overlay": json.loads(row["overlay_json"]) if row and row["overlay_json"] not in (None, "{}")
                   else OverlaySettings().model_dump(),
        "duration_sec": cfg.default_duration_sec,
    }


@app.post("/api/jobs/preview")
async def preview_job(req: PreviewRequest) -> dict:
    """実行せずコマンドだけ返す。開始前に何が走るのかを見せる。"""
    url, res, token = _resolve_target_input(req.target_url, req.target_id, req.bearer_token)
    encode = _resolve_encode(req)
    _check_source_compatibility(encode, req.source)
    _check_overlay(encode, req.overlay)
    _check_source(req.source)

    encode, encoder_notes = _apply_encoder_availability(encode)
    source, seam_frames, source_notes = _prepare_source(req.source, encode)
    source_notes = [*encoder_notes, *source_notes]
    duration = req.duration_sec if req.duration_sec is not None else cfg.default_duration_sec
    argv = build_command(
        ffmpeg_path=cfg.ffmpeg_path,
        protocol=res.protocol,
        target_url=url,
        source=source,
        seam_frames=seam_frames,
        encode=encode,
        duration_sec=duration,
        overlay=req.overlay,
        font_path=_font_path(),
        job_label=req.target_label,
        media_dir=str(cfg.media_dir),
        bearer_token=token,
        webrtc_delivery=req.webrtc_delivery,
        # 実際に走るコマンドと1文字も違わないようにする（憲章 Principle 5）
        snapshot=_snapshot_spec(),
    )
    # 秘匿値はプレビューにも出さない
    display = get_targets().display_url_for(url, res)
    _, adaptations = adapt_encode(encode, res.protocol, req.webrtc_delivery)
    adaptations = [*adaptations, *source_notes]
    return {
        "protocol": res.protocol,
        "adaptations": adaptations,
        "command": redact(
            to_shell(argv), url=url, display_url=display, values=res.secrets.values()
        ),
    }


@app.post("/api/jobs/benchmark")
async def benchmark_job(req: PreviewRequest) -> dict:
    """同じ設定で数秒だけ回し、実時間の何倍で処理できるかを測る。

    送出はしない（出力を捨てる）ので、配信先には影響しない。
    「この設定で落ちるか」を、目安ではなく実測で答えるための機能。
    """
    if get_runner().is_running():
        raise HTTPException(
            status_code=409, detail="送出中は計測できません。先に停止してください"
        )

    url, res, token = _resolve_target_input(req.target_url, req.target_id, req.bearer_token)
    encode = _resolve_encode(req)
    _check_source_compatibility(encode, req.source)
    _check_source(req.source)
    encode, _ = _apply_encoder_availability(encode)
    source, seam_frames, _ = _prepare_source(req.source, encode)

    argv = build_command(
        ffmpeg_path=cfg.ffmpeg_path,
        protocol=res.protocol,
        target_url=url,
        source=source,
        seam_frames=seam_frames,
        encode=encode,
        duration_sec=None,
        overlay=req.overlay,
        font_path=_font_path(),
        job_label=req.target_label,
        media_dir=str(cfg.media_dir),
        bearer_token=token,
        webrtc_delivery=req.webrtc_delivery,
        # 計測にプレビューは要らない。付けると出力が2本になり、
        # 出力を差し替える to_null_output()（末尾3つが `-f <muxer> <url>` 前提）が壊れる
        snapshot=None,
        # 実時間に絞ったまま測ると必ず 1.0 倍になり、「余裕があるか」を答えられない
        pacing=False,
    )
    return await benchmark.measure(argv)


@app.post("/api/jobs", status_code=201)
async def create_job(req: JobCreate) -> dict:
    caps = state["caps"]
    if not caps.available:
        raise HTTPException(status_code=503, detail=caps.error)

    url, res, token = _resolve_target_input(req.target_url, req.target_id, req.bearer_token)
    encode = _resolve_encode(req)
    _check_source_compatibility(encode, req.source)
    _check_overlay(encode, req.overlay)
    _check_source(req.source)
    encode, _ = _apply_encoder_availability(encode)
    source, seam_frames, _ = _prepare_source(req.source, encode)

    # 送出したURLは必ず送出先として残す。次回はワンクリックで選べる
    target = get_targets().save(
        url, res, label=req.target_label, touch=True,
        bearer_token=req.bearer_token or None,
    )

    # エンコード設定も同時にテンプレート化する。ここが「保存操作をなくす」実体で、
    # 送出開始の瞬間にだけ走らせるのは、触っただけの中間状態を残さないため
    template = get_templates().record_use(encode)

    try:
        job_id = await get_runner().start(
            req,
            res.protocol,
            url=url,
            secrets=list(res.secrets.values()),
            target_id=target["id"],
            template_id=template["id"],
            display_url=target["display_url"],
            encode=encode,
            overlay=req.overlay,
            font_path=_font_path(),
            bearer_token=token,
            webrtc_delivery=req.webrtc_delivery,
            source=source,
            seam_frames=seam_frames,
        )
    except runner.JobBusy as exc:
        # 同時稼働は1本だけ。停止して差し替えるかは UI 側で確認する
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"FFmpeg を起動できませんでした: {exc}") from exc

    return runner.load_job(state["conn"], job_id) or {"id": job_id}


@app.get("/api/jobs")
async def list_jobs(limit: int = 50) -> dict:
    job_runner = get_runner()
    return {
        "running_job_id": job_runner.current_job_id if job_runner.is_running() else None,
        "jobs": runner.list_jobs(state["conn"], limit),
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int) -> dict:
    job = runner.load_job(state["conn"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    job["log"] = runner.read_log_tail(job.get("log_path"), cfg.log_tail_bytes)
    return job


@app.get("/api/jobs/{job_id}/report")
async def job_report(job_id: int, log: str = "tail") -> Response:
    """テスト結果を1ファイルのJSONにまとめて返す。

    設定・経時の指標・指摘の履歴・ログをまとめる。秘匿値はDBに入っている
    時点で伏せ字済みのものだけを使う。`log=full` で全行、`log=none` で省略。
    """
    lines = {"full": None, "none": 0}.get(log, report.DEFAULT_LOG_LINES)
    data = report.build(
        state["conn"], job_id, log_lines=lines, ffmpeg_version=state["caps"].version
    )
    if data is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")

    return Response(
        content=report.dumps(data),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{report.filename(data["job"])}"'
        },
    )


@app.delete("/api/jobs/{job_id}")
async def stop_job(job_id: int) -> dict:
    job_runner = get_runner()
    if job_runner.current_job_id != job_id or not job_runner.is_running():
        raise HTTPException(status_code=409, detail="そのジョブは送出中ではありません")
    await job_runner.stop()
    return {"stopped": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}/snapshot.jpg")
async def job_snapshot(job_id: int) -> Response:
    """セルフプレビューの最新の1枚。

    ファイルを読んでから返すのは、`-atomic_writing 1`（一時ファイル＋rename）と
    組で**書きかけを掴まない**ことを保証するため。開いたまま返すと、送り出す
    途中で差し替えられる余地が残る。1枚10KB程度なので読んでも安い。

    `X-Snapshot-Age` は最後に書かれてからの秒数。画面はこれを出して、
    「いつの絵か」を隠さない（止まっていることを止まっていると見せる）。
    """
    job_runner = get_runner()
    if job_runner.current_job_id != job_id or not job_runner.is_running():
        raise HTTPException(status_code=404, detail="そのジョブは送出中ではありません")
    try:
        data = cfg.snapshot_path.read_bytes()
        age = max(0.0, time.time() - cfg.snapshot_path.stat().st_mtime)
    except OSError:
        raise HTTPException(status_code=404, detail="まだ1枚も出ていません") from None
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Snapshot-Age": f"{age:.1f}"},
    )


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: int) -> StreamingResponse:
    """SSE。購読直後に現在値を1回流してから、以後の差分を送る。"""
    job_runner = get_runner()
    queue = job_runner.subscribe()

    async def stream():
        try:
            yield _sse("snapshot", job_runner.snapshot())
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # プロキシに切られないように
                    continue
                yield _sse(payload["event"], payload["data"])
        except asyncio.CancelledError:
            raise
        finally:
            job_runner.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
