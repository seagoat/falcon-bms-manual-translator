"""Lead: why does the merge see only 9431 of 11128 translations for TO-34?"""
import sqlite3
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

DB = "data/_to_probe/app.db"
st = sqlite3.connect(DB)
st.row_factory = sqlite3.Row

v = st.execute("SELECT * FROM document_versions WHERE version_id=1").fetchone()
print("version:", dict(v))
vid = 1
n_body = st.execute("SELECT COUNT(*) FROM segments WHERE version_id=? AND role='body'",
                    (vid,)).fetchone()[0]
n_all = st.execute("SELECT COUNT(*) FROM segments WHERE version_id=?", (vid,)).fetchone()[0]
print(f"segments: all={n_all} body={n_body}")

n_tr = st.execute(
    "SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.version_id=?", (vid,)).fetchone()[0]
n_tr_body = st.execute(
    "SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.version_id=? AND s.role='body'", (vid,)).fetchone()[0]
print(f"translations: all={n_tr} body={n_tr_body}")

n_empty = st.execute(
    "SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.version_id=? AND s.role='body' AND (t.text IS NULL OR t.text='')",
    (vid,)).fetchone()[0]
print(f"body 译文中空文本: {n_empty}")

# 这些空文本是哪些段？
print("\n空文本译文样例:")
for r in st.execute(
        "SELECT s.id, s.page, s.kind, s.text, t.status FROM translations t "
        "JOIN segments s ON s.id=t.segment_id WHERE s.version_id=? AND s.role='body' "
        "AND (t.text IS NULL OR t.text='') LIMIT 12", (vid,)):
    print(f"  seg={r['id']} p{r['page']} {r['kind']:10s} EN={r['text'][:40]!r} [{r['status']}]")

# fingerprint 重复情况
fps = st.execute(
    "SELECT s.fingerprint AS fp, COUNT(*) n FROM translations t "
    "JOIN segments s ON s.id=t.segment_id WHERE s.version_id=? AND s.role='body' "
    "AND s.fingerprint<>'' GROUP BY s.fingerprint HAVING n>1 LIMIT 8", (vid,)).fetchall()
print(f"\nfingerprint 重复组样例: {len(fps)}")
for r in fps:
    print(f"  fp={r['fp'][:12]} x{r['n']}")

n_unique = st.execute(
    "SELECT COUNT(DISTINCT s.fingerprint) FROM translations t "
    "JOIN segments s ON s.id=t.segment_id WHERE s.version_id=? AND s.role='body' "
    "AND s.fingerprint<>''", (vid,)).fetchone()[0]
print(f"\nbody 译文里不同 fingerprint 数: {n_unique}")
st.close()
