# instabox waker

Repo private ini menjalankan `external-waker.py` lewat GitHub Actions tiap 20 menit:
wake kalau sandbox paused, dan suspend+resume sendiri di menit 450 supaya tidak
kena max-run (480 menit).

Secret yang dibutuhkan: `INSTABOX_API_KEY` (Settings → Secrets and variables → Actions).
