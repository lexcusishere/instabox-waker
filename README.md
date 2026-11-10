# Wake cycler: rantai job GitHub Actions (v3, 2026-11-04)

## Kenapa cron GitHub dibuang

Terukur dari repo `lexcusishere/instabox-waker` (API GitHub, `actions/runs`):

- 8 run `schedule` dalam 21 jam. Kalau cron `*/5` benar-benar jalan, harusnya ~250 run.
- Gap rata-rata **145 menit**, terburuk **475 menit**.

Efeknya nyata di box (`GET /v1/machines/{id}/logs` → `operations[]`):

| waktu suspend | trigger | umur run saat suspend |
|---|---|---|
| 2026-11-04T02:27:23Z | **max_run** | 480 menit  ← cycler gagal total |
| 2026-11-04T11:14:03Z | manual/API | 477 menit  ← nyaris kena |
| 2026-11-04T13:23:49Z | manual/API | 130 menit  ← itu tes `--safe-minutes 60` |

Setiap `trigger=max_run` = cooldown free plan jalan (5–20m, lalu 60–75m, lalu 120–135m).

## Arsitektur sekarang

| workflow | peran | ambang cycle | penggerak |
|---|---|---|---|
| `instabox-waker-chain.yml` | **cycler utama** | 420m | loop internal 68 × 5 menit, lalu dispatch diri sendiri |
| `instabox-waker-backup.yml` | jaring kedua (independen) | 440m | loop internal 68 × 5 menit + dispatch diri sendiri |
| `instabox-waker.yml` | wake-only + net ketiga | 430m | cron (best-effort, tidak diandalkan) |
| `chain-smoke.yml` | diagnosa manual 1 iterasi | – | `workflow_dispatch` |
| `chain-probe.yml` | bukti `GITHUB_TOKEN` boleh dispatch | – | `workflow_dispatch` |

Urutan ambang dijaga: 420 (rantai) → 430 (net cron) → 440 (backup) → 480 (max-run keras).
Karena rantai utama reset window di 420, ambang 430 dan 440 **normalnya tidak pernah kena**;
keduanya hanya hidup kalau rantai utama mati.

## Fakta yang sudah dibuktikan

1. `GITHUB_TOKEN` bawaan Actions boleh men-dispatch workflow di repo yang sama asal job punya
   `permissions: actions: write` → `chain-probe` mencetak `dispatch code=204`. Tidak perlu PAT.
2. Payload rantai benar-benar membaca API instabox (bukan fallback waktu lokal):
   `chain-smoke` mencetak `runStartedAt dari API: 2026-11-04T13:24:44.568454Z (128 menit berjalan)`.
3. Loop menidurkan 300 detik, bukan crash-loop: run rantai tetap satu-satunya `in_progress`
   dan umurnya >14 menit tanpa ada run baru di antrean. Kalau step crash, step 4
   (`if: always()`) langsung men-dispatch dan job selesai.
4. Batas keras job GitHub Actions 6 jam → loop dibatasi 68 × 5 menit (≈5j40m) lalu handover.

## Cara memeriksa (tanpa buka UI)

```bash
T=$(cat /data/workspace/instabox-keepalive/.ghtok)
curl -s -H "Authorization: Bearer $T" \
  "https://api.github.com/repos/lexcusishere/instabox-waker/actions/runs?per_page=15" \
 | python3 -c 'import sys,json;[print(r["id"],r["name"],r["status"],r.get("conclusion")) for r in json.load(sys.stdin)["workflow_runs"]]'
```

Harapan sehat: **tepat satu** `instabox waker chain` dan **tepat satu** `instabox waker backup`
berstatus `in_progress`, tidak ada `queued` menumpuk.

## Bukti di sisi mesin

```bash
cd /data/workspace/instabox-keepalive && set -a && . ./api.env && set +a
curl -s -H "X-API-Key: $INSTABOX_API_KEY" \
  "https://api.instabox.sh/v1/machines/m_agq4deh4qasrdcchs4amnqhele/logs?limit=1" \
 | python3 -c 'import sys,json;d=json.load(sys.stdin);[print(o["createdAt"][:19],o["type"],o.get("trigger")) for o in d["operations"] if o["type"] in ("suspend","resume")][-10:]'
```

Yang diharapkan muncul setelah ini: pasangan `suspend manual/API` pada umur ~420 menit
(dari rantai) dan **tidak ada lagi** `suspend max_run`.

## Kalau rantai putus

1. Dispatch manual: `POST .../actions/workflows/instabox-waker-chain.yml/dispatches` `{"ref":"main"}`.
2. Kalau cron `6,16,...` di file rantai masih hidup, dia akan bootstrap sendiri tiap jam.
3. Darurat total (GitHub mati/di-nonaktifkan): Google Apps Script
   `/data/workspace/keepalive-kit/instabox-keepalive/wake-outside/google-apps-script/Code.v2.personal.gs`
   (SAFE_MINUTES 400, sudah lolos 21/21 uji di `test-code.gs.js`), tinggal ditempel ke
   script.google.com lalu jalankan `setup()` sekali.

## Token

`/data/workspace/instabox-keepalive/.ghtok` (0600) = classic PAT `lexcusishere` yang masih
valid, dipakai untuk push/dispatch manual dari box ini. Workflow sendiri TIDAK memakai PAT
(memakai `secrets.GITHUB_TOKEN`).
