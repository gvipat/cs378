"""gpu-lease: a small control plane that leases EC2 GPU instances to student groups.

Everything runs on one box: `api` (FastAPI) serves the students, `reaper` runs as
a thread inside it, and the whole database is one SQLite file under var/.
"""
