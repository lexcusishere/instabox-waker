#!/usr/bin/env python3
"""
external-waker v2 — penjaga max-run instabox (free plan).

PERUBAHAN v2 (2026-11-06) — PENTING
  maxRunMinutes free plan TIDAK LAGI 480 menit. Mesin ini terukur 120 menit
  (8 jam -> 2 jam; API mesin: "maxRunMinutes": 120, dan `operations[]` mencatat
  suspend trigger=max_run pada umur run 120,6 menit). Dokumentasi instabox.sh
  (docs/limits.md, docs/idle-suspend.md) masih menulis 480 — DOKUMENTASI BASI.
  Sumber kebenaran satu-satunya: GET /v1/machines/{id} -> maxRunMinutes.

  Karena itu ambang cycling TIDAK BOLEH di-hardcode. Default sekarang
  --safe-minutes auto = maxRunMinutes - margin (margin 30), dihitung ULANG
  setiap kali script jalan. Kalau plan berubah lagi (240/480/unlimited),
  script ikut sendiri tanpa diedit.

  Tambahan pengaman: --hard-margin (default 10). Walaupun soft cycling
  dimatikan (--safe-minutes 0) atau salah set terlalu besar, script tetap
  suspend+resume saat run mencapai maxRun - hard_margin, supaya box tidak
  pernah kena hukuman cooldown max-run. Matikan dengan --no-hard-guard.

KENAPA HARUS DARI LUAR?
  Max-run mem-pause sandbox walau sedang aktif. Setelah ter-pause, tidak ada
  proses di dalam box yang bisa jalan (box-nya mati). Jadi yang harus membangunkan
  harus berada DI LUAR box: laptopmu, VPS lain, GitHub Actions, cron HP, dll.

STRATEGI ANTI-COOLDOWN (ini inti trik-nya)
  Cooldown setelah max-run pause bertingkat di free plan:
     pause max-run ke-1 : 5-20 menit
     pause max-run ke-2 : 60-75 menit     <-- hukuman
     pause max-run ke-3+: 120-135 menit   <-- makin sakit
  TAPI: "The count starts over when a run ends with no cooldown owed
  (idle pause, or YOU PAUSE OR STOP the machine)".
  => Jangan biarkan box kena max-run. Sebelum window habis, kita SUSPEND
     sendiri dari luar (manual pause = tidak ada cooldown, counter balik ke 0),
     lalu langsung RESUME. Run timer mulai dari 0 lagi -> window baru.

CARA PAKAI
  export INSTABOX_API_KEY=ibk_xxx          # buat di dashboard > API keys
  python3 external-waker.py --machine m_xxx
  # atau biarkan dia cari sendiri kalau cuma punya 1 mesin:
  python3 external-waker.py

OPSI
  --machine ID|NAMA     id/name sandbox (default: auto kalau akunmu hanya punya 1)
  --safe-minutes N|auto suspend+resume sendiri saat run mencapai N menit.
                        default "auto" = maxRunMinutes - margin.
                        0 = matikan soft cycling (hard guard tetap jalan).
  --margin N            jarak aman untuk mode auto (default 30 menit)
  --hard-margin N       pengaman terakhir: cycle di maxRun - N (default 10)
  --no-hard-guard       matikan pengaman terakhir
  --poll N              detik antar cek (default 120)
  --cooldown-margin N   tambahan detik setelah cooldownUntil lewat (default 30)
  --once                satu kali cek lalu keluar (buat cron / debugging)
  --dry-run             tidak mengirim perintah lifecycle, hanya cetak rencana
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

API = os.environ.get("INSTABOX_API_URL", "https://api.instabox.sh").rstrip("/")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [waker] {msg}", flush=True)


def api(method: str, path: str, key: str, body=None, idem=False, timeout=30):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", key)
    if data:
        req.add_header("Content-Type", "application/json")
    if idem:
        req.add_header("Idempotency-Key", str(uuid.uuid4()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {}), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return e.code, parsed, dict(e.headers)
    except Exception as e:  # network, DNS, timeout
        return 0, {"error": {"code": "transport", "message": str(e)}}, {}


def pick_machine(key: str, wanted: str | None):
    if wanted:
        return wanted
    code, body, _ = api("GET", "/v1/machines", key)
    if code != 200:
        log(f"gagal list machines: http {code} {body}")
        return None
    items = body.get("machines") or body.get("items") or body.get("data") or []
    if isinstance(body, list):
        items = body
    if len(items) == 1:
        m = items[0]
        mid = m.get("id") or m.get("machineId")
        log(f"auto-pilih mesin: {mid} ({m.get('name')})")
        return mid
    log(f"ada {len(items)} mesin — pakai --machine untuk memilih satu")
    for m in items:
        log(f"   - {(m.get('id'))}  name={m.get('name')}  state={m.get('state')}")
    return None


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def seconds_left(until_iso):
    dt = parse_ts(until_iso)
    if not dt:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds()


def get_machine(key, mid):
    code, body, _ = api("GET", f"/v1/machines/{mid}", key)
    if code != 200:
        return None, body
    return body, None


def op_wait(key, op_id, timeout=180):
    """tunggu operasi selesai (operations endpoint)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, body, _ = api("GET", f"/v1/operations/{op_id}", key)
        if code != 200:
            return False, body
        st = body.get("state")
        if st in ("succeeded", "failed", "cancelled"):
            return st == "succeeded", body
        time.sleep(3)
    return False, {"error": {"code": "op_timeout"}}


def do(key, mid, action, dry, wait=True):
    """action: start|stop|suspend|resume|wake"""
    if dry:
        log(f"DRY-RUN: POST /v1/machines/{mid}/{action}")
        return True
    path = f"/v1/machines/{mid}/{action}" if action != "wake" else f"/v1/machines/{mid}/wake"
    code, body, hdrs = api("POST", path, key, body={} if action != "wake" else None, idem=True)
    if code in (200, 202):
        op = body.get("id") or body.get("operationId")
        log(f"{action}: http {code} op={op}")
        if wait and op:
            ok, res = op_wait(key, op)
            log(f"{action}: operasi {'succeeded' if ok else 'FAILED'} {res.get('error') or ''}")
            return ok
        return True
    err = body.get("error", {}) if isinstance(body, dict) else {}
    retry_after = err.get("retryAfterSeconds") or hdrs.get("Retry-After")
    log(f"{action}: http {code} code={err.get('code')} retryAfter={retry_after} :: {err.get('message')}")
    if err.get("code") == "machine_cooldown" and retry_after:
        log(f"cooldown aktif, tunggu {retry_after}s")
        if not dry:
            time.sleep(float(retry_after) + 5)
    return False


def cycle(key, mid, dry, run_min, maxrun, why):
    """suspend+resume sendiri: pause manual tidak berutang cooldown, run timer reset."""
    log(f"run {run_min:.0f}m / maxRun {maxrun}m — {why} -> suspend+resume SENDIRI "
        f"(hindari hukuman cooldown max-run)")
    if do(key, mid, "suspend", dry):
        time.sleep(10)
        ok = do(key, mid, "resume", dry)
        if ok:
            log(f"window {maxrun} menit baru dimulai (runStartedAt di-reset)")
        else:
            log("resume GAGAL — biarkan percobaan berikutnya / self-healing job yang membangunkan")
        return ok
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--machine", default=os.environ.get("INSTABOX_MACHINE"))
    ap.add_argument("--safe-minutes", default="auto",
                    help='"auto" (= maxRunMinutes - margin) atau angka menit; 0 = matikan soft cycling')
    ap.add_argument("--margin", type=float, default=30.0,
                    help="jarak aman (menit) untuk --safe-minutes auto")
    ap.add_argument("--hard-margin", type=float, default=10.0,
                    help="pengaman terakhir: cycle di maxRun - N menit")
    ap.add_argument("--no-hard-guard", action="store_true")
    ap.add_argument("--poll", type=int, default=120)
    ap.add_argument("--cooldown-margin", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("INSTABOX_API_KEY")
    if not key:
        log("INSTABOX_API_KEY tidak di-set. Buat di dashboard > API keys, lalu export.")
        sys.exit(2)

    mid = pick_machine(key, a.machine)
    if not mid:
        sys.exit(2)

    run_started = None  # wall-clock saat waker mulai menganggap run ini berjalan
    while True:
        body, err = get_machine(key, mid)
        if err:
            log(f"get machine gagal: {err.get('error')}")
            if a.once:
                return
            time.sleep(a.poll)
            continue

        state = body.get("state")
        cooldown_left = seconds_left(body.get("cooldownUntil"))
        auto_susp = body.get("autoSuspendedAt")
        maxrun = body.get("maxRunMinutes")
        log(f"state={state} maxRun={maxrun} cooldownUntil={body.get('cooldownUntil')} "
            f"autoSuspendedAt={auto_susp}")

        if cooldown_left and cooldown_left > 0:
            wait_s = cooldown_left + a.cooldown_margin
            log(f"dalam cooldown {int(cooldown_left)}s — tidur {int(wait_s)}s dulu")
            if a.once:
                return
            time.sleep(wait_s)
            continue

        if state == "stopped":
            do(key, mid, "start", a.dry_run)
            run_started = time.time()
        elif state in ("suspended", "paused"):
            # mesin yang pause sendiri (idle) -> wake; yang kita pause sendiri -> resume
            if auto_susp:
                do(key, mid, "wake", a.dry_run)
            else:
                do(key, mid, "resume", a.dry_run)
            run_started = time.time()
        elif state == "running":
            if run_started is None:
                # PENTING: jangan pakai waktu lokal waker. Pakai runStartedAt dari API,
                # kalau tidak, waker yang baru start akan mengira run baru 0 menit
                # dan box keburu kena max-run (cooldown).
                rs = body.get("runStartedAt")
                if rs:
                    t = parse_ts(rs)
                    if t:
                        run_started = t.timestamp()
                        log(f"runStartedAt dari API: {rs} ({(time.time()-run_started)/60:.0f} menit berjalan)")
                    else:
                        log(f"runStartedAt tidak bisa diparse ({rs}), fallback ke waktu lokal")
                        run_started = time.time()
                else:
                    run_started = time.time()
            run_min = (time.time() - run_started) / 60.0

            # -------- tentukan ambang: soft (safe) + hard guard --------
            unlimited = (maxrun is None or int(maxrun) < 0)
            safe = None
            if str(a.safe_minutes).strip().lower() == "auto":
                if not unlimited and int(maxrun) > 0:
                    safe = max(5.0, float(maxrun) - a.margin)
            else:
                try:
                    safe = float(a.safe_minutes)
                except ValueError:
                    safe = None
                if safe is not None and safe <= 0:
                    safe = None

            hard = None
            if not a.no_hard_guard and not unlimited and maxrun and int(maxrun) > 0:
                hard = max(1.0, float(maxrun) - a.hard_margin)

            if safe and not unlimited and safe >= float(maxrun):
                log(f"PERINGATAN: safe ({safe:.0f}m) >= maxRun ({maxrun}m) — "
                    f"soft cycling tidak akan pernah kena; pakai --safe-minutes auto")

            if unlimited:
                log(f"maxRun=-1 (unlimited) — tidak perlu cycling. run {run_min:.0f} menit")
            elif safe and run_min >= safe:
                cycle(key, mid, a.dry_run, run_min, maxrun, f"run >= safe {safe:.0f}m")
                run_started = time.time()
            elif hard and run_min >= hard:
                cycle(key, mid, a.dry_run, run_min, maxrun,
                      f"HARD GUARD: run >= maxRun-{a.hard_margin:.0f} ({hard:.0f}m)")
                run_started = time.time()
            elif safe:
                log(f"run {run_min:.0f} menit — di bawah safe {safe:.0f}m "
                    f"(cycle dalam ~{safe - run_min:.0f} menit, max-run {maxrun}m)")
            else:
                left = (float(maxrun) - run_min) if maxrun else float("nan")
                log(f"mode watchdog (soft cycling mati): run {run_min:.0f} menit, "
                    f"sisa ~{left:.0f} menit sebelum max-run"
                    + (f", hard guard di {hard:.0f}m" if hard else ", TANPA hard guard"))

        if a.once:
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("dihentikan user")
