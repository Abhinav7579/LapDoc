import sqlite3
c = sqlite3.connect("monitor.db")
print("Total samples:", c.execute("select count(*) from samples").fetchone()[0])
print("Latest:", c.execute("select * from samples order by id desc limit 1").fetchone())