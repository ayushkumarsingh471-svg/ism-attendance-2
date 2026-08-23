import os
import io
import re
import base64
import calendar
from datetime import datetime, date
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

app = FastAPI(title="ISM Attendance ERP - Final Full Edition")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    try:
        import streamlit as st
        DATABASE_URL = st.secrets["DATABASE_URL"]
    except:
        DATABASE_URL = "postgresql://postgres.parhsaqmmmiyojwkhsrn:%40fr3rdEyp.%2B%25ug%3D@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, poolclass=NullPool)

def init_master_db():
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS master_users (
                username TEXT PRIMARY KEY, 
                password TEXT
            )
        '''))

init_master_db()

def get_safe_prefix(uid):
    clean = "".join(c for c in str(uid) if c.isalnum() or c == '_').lower()
    if not clean or clean[0].isdigit():
        clean = "u_" + clean
    return clean

def sort_students_safely(students):
    def safe_roll_key(s):
        try:
            return int(''.join(filter(str.isdigit, str(s[2]))))
        except:
            return str(s[2])
    return sorted(students, key=safe_roll_key)

def init_tenant_db(user_id):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    t_settings = f"{safe_uid}_settings"
    t_details = f"{safe_uid}_student_details"
    t_leaves = f"{safe_uid}_leaves"

    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE IF NOT EXISTS {t_students} (id SERIAL PRIMARY KEY, reg_no TEXT UNIQUE, roll_no TEXT, name TEXT)'))
        conn.execute(text(f'CREATE TABLE IF NOT EXISTS {t_subjects} (id SERIAL PRIMARY KEY, subject_name TEXT UNIQUE)'))
        conn.execute(text(f'CREATE TABLE IF NOT EXISTS {t_attendance} (id SERIAL PRIMARY KEY, student_id INTEGER, subject_id INTEGER, date TEXT, status TEXT, UNIQUE(student_id, subject_id, date))'))
        conn.execute(text(f'CREATE TABLE IF NOT EXISTS {t_settings} (key TEXT PRIMARY KEY, value TEXT)'))
        conn.execute(text(f'''CREATE TABLE IF NOT EXISTS {t_details} (
            reg_no TEXT PRIMARY KEY,
            email TEXT,
            contact TEXT,
            parent_name TEXT,
            parent_contact TEXT,
            res_type TEXT,
            photo_data TEXT
        )'''))
        # Leave Table (Without Document Columns for faster loading)
        conn.execute(text(f'''CREATE TABLE IF NOT EXISTS {t_leaves} (
            id SERIAL PRIMARY KEY,
            reg_no TEXT,
            student_name TEXT,
            leave_type TEXT,
            subject TEXT,
            from_date TEXT,
            to_date TEXT,
            reason TEXT,
            status TEXT DEFAULT 'Pending',
            faculty_remark TEXT DEFAULT '',
            applied_on TEXT
        )'''))

        conn.execute(text(f'CREATE INDEX IF NOT EXISTS idx_{safe_uid}_att_sub_dt ON {t_attendance}(subject_id, date)'))
        conn.execute(text(f'CREATE INDEX IF NOT EXISTS idx_{safe_uid}_att_st_stat ON {t_attendance}(student_id, status)'))

        try:
            conn.execute(text(f'ALTER TABLE {t_details} ADD COLUMN IF NOT EXISTS photo_data TEXT'))
        except:
            pass

        res = conn.execute(text(f"SELECT COUNT(*) FROM {t_subjects}")).fetchone()[0]
        if res == 0:
            for sub in ['SAD', 'PST&PC', 'NT', 'BE', 'OS&UNIX LAB', 'PROG IN C LAB']:
                conn.execute(text(f"INSERT INTO {t_subjects} (subject_name) VALUES (:sub) ON CONFLICT DO NOTHING"), {"sub": sub})

# ==========================================
# AUTHENTICATION API
# ==========================================

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    u = username.strip()
    with engine.begin() as conn:
        res = conn.execute(text("SELECT * FROM master_users WHERE username=:u AND password=:p"), {"u": u, "p": password}).fetchone()
    if res:
        init_tenant_db(u)
        return {"success": True, "user": u}
    raise HTTPException(status_code=400, detail="Invalid Faculty ID or Password.")

@app.post("/api/register")
def register(username: str = Form(...), password: str = Form(...)):
    u = username.strip()
    if not u or not password:
        raise HTTPException(status_code=400, detail="Both fields are required.")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO master_users (username, password) VALUES (:u, :p)"), {"u": u, "p": password})
        init_tenant_db(u)
        return {"success": True}
    except Exception:
        raise HTTPException(status_code=400, detail="Faculty ID already exists. Please choose another.")

@app.post("/api/student_login")
def student_login(reg_no: str = Form(...), name: str = Form(...)):
    r_no = reg_no.strip()
    s_name = name.strip()

    with engine.begin() as conn:
        faculties = conn.execute(text("SELECT username FROM master_users")).fetchall()
        for fac in faculties:
            f_id = fac[0]
            safe_uid = get_safe_prefix(f_id)
            t_students = f"{safe_uid}_students"
            try:
                conn.execute(text(f"SELECT 1 FROM {t_students} LIMIT 1"))
                st = conn.execute(text(f"SELECT id, name, roll_no FROM {t_students} WHERE LOWER(reg_no)=LOWER(:r) AND LOWER(name)=LOWER(:n)"), {"r": r_no, "n": s_name}).fetchone()
                if st:
                    return {"success": True, "faculty_id": f_id, "reg_no": r_no, "name": st[1]}
            except Exception:
                continue

    raise HTTPException(status_code=400, detail="Student not found. Please check your Registration No and exact Name spelling.")

@app.get("/api/student_dashboard_data/{faculty_id}/{reg_no:path}")
def get_student_dashboard_data(faculty_id: str, reg_no: str):
    init_tenant_db(faculty_id)
    
    safe_uid = get_safe_prefix(faculty_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    t_settings = f"{safe_uid}_settings"
    t_leaves = f"{safe_uid}_leaves"

    with engine.begin() as conn:
        st = conn.execute(text(f"SELECT id, name, roll_no FROM {t_students} WHERE LOWER(reg_no)=LOWER(:r)"), {"r": reg_no}).fetchone()
        if not st: return {"error": "Student not found"}
        st_id, st_name, st_roll = st[0], st[1], st[2]

        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        sub_map = {r[1]: r[0] for r in sub_rows}

        sub_counts = conn.execute(text(f"SELECT subject_id, COUNT(DISTINCT date) FROM {t_attendance} GROUP BY subject_id")).fetchall()
        sub_total_classes = {r[0]: r[1] for r in sub_counts}

        att_rows = conn.execute(text(f"SELECT subject_id, COUNT(*) FROM {t_attendance} WHERE student_id=:sid AND status='Present' GROUP BY subject_id"), {"sid": st_id}).fetchall()
        present_map = {r[0]: r[1] for r in att_rows}

        summary = []
        tot_p_all = 0
        tot_c_all = 0
        for sub, sid in sub_map.items():
            tot_c = sub_total_classes.get(sid, 0)
            tot_p = present_map.get(sid, 0)
            tot_p_all += tot_p
            tot_c_all += tot_c
            pct = round((tot_p / tot_c * 100)) if tot_c > 0 else 0
            summary.append({"subject": sub, "present": tot_p, "total": tot_c, "pct": pct})

        overall_pct = round((tot_p_all / tot_c_all * 100)) if tot_c_all > 0 else 0

        recent_records = conn.execute(text(f"""
            SELECT sub.subject_name, a.date, a.status 
            FROM {t_attendance} a 
            JOIN {t_subjects} sub ON a.subject_id = sub.id 
            WHERE a.student_id = :sid 
            ORDER BY a.date DESC
        """), {"sid": st_id}).fetchall()

        history = [{"subject": r[0], "date": r[1], "status": r[2]} for r in recent_records]

        leaves_list = []
        try:
            leaves_raw = conn.execute(text(f"""
                SELECT id, leave_type, subject, from_date, to_date, reason, status, faculty_remark, applied_on 
                FROM {t_leaves} 
                WHERE LOWER(reg_no)=LOWER(:r) 
                ORDER BY id DESC
            """), {"r": reg_no.strip()}).fetchall()

            leaves_list = [{
                "id": l[0], "leave_type": l[1], "subject": l[2], "from_date": l[3], "to_date": l[4],
                "reason": l[5], "status": l[6], "faculty_remark": l[7] or "", "applied_on": l[8]
            } for l in leaves_raw]
        except Exception:
            leaves_list = []

        def get_cfg(k, def_v):
            res = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key=:k"), {"k": k}).fetchone()
            return res[0] if res and res[0] else def_v

        return {
            "student": {"name": st_name, "reg_no": reg_no, "roll_no": st_roll},
            "overall_pct": overall_pct,
            "summary": summary,
            "history": history,
            "leaves": leaves_list,
            "college_name": get_cfg('college_name', 'INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)'),
            "course": get_cfg('course_name', 'BCA') + " - " + get_cfg('section_name', 'Semester 1'),
            "logo": get_cfg('college_logo', 'https://i.ibb.co/3s68K1v/tree-logo.png')
        }

# ==========================================
# LEAVE MANAGEMENT API
# ==========================================

@app.post("/api/apply_leave")
async def apply_leave(
    faculty_id: str = Form(...),
    reg_no: str = Form(...),
    student_name: str = Form(...),
    leave_type: str = Form(...),
    subject: str = Form(...),
    from_date: str = Form(...),
    to_date: str = Form(...),
    reason: str = Form(...)
):
    try:
        safe_uid = get_safe_prefix(faculty_id)
        t_leaves = f"{safe_uid}_leaves"
        
        cur_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {t_leaves} (reg_no, student_name, leave_type, subject, from_date, to_date, reason, status, faculty_remark, applied_on)
                VALUES (:r, :n, :lt, :sub, :fd, :td, :rs, 'Pending', '', :app_on)
            """), {
                "r": reg_no.strip(), "n": student_name.strip(), "lt": leave_type, "sub": subject,
                "fd": from_date, "td": to_date, "rs": reason,
                "app_on": cur_date_str
            })
        return {"success": True, "message": "Leave application sent successfully to your Faculty Portal!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to apply leave: " + str(e))

@app.get("/api/leaves/{user_id}")
def get_faculty_leaves(user_id: str):
    safe_uid = get_safe_prefix(user_id)
    t_leaves = f"{safe_uid}_leaves"
    with engine.begin() as conn:
        rows = conn.execute(text(f"""
            SELECT id, reg_no, student_name, leave_type, subject, from_date, to_date, reason, status, faculty_remark, applied_on
            FROM {t_leaves}
            ORDER BY id DESC
        """)).fetchall()

        leaves = [{
            "id": r[0], "reg_no": r[1], "student_name": r[2], "leave_type": r[3], "subject": r[4],
            "from_date": r[5], "to_date": r[6], "reason": r[7], 
            "status": r[8], "faculty_remark": r[9] or "", "applied_on": r[10]
        } for r in rows]
    return {"leaves": leaves}

@app.post("/api/update_leave_status")
def update_leave_status(user_id: str = Form(...), leave_id: int = Form(...), status: str = Form(...), remark: str = Form("")):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_leaves = f"{safe_uid}_leaves"
        with engine.begin() as conn:
            conn.execute(text(f"""
                UPDATE {t_leaves} 
                SET status = :st, faculty_remark = :rem 
                WHERE id = :lid
            """), {"st": status, "rem": remark.strip(), "lid": leave_id})
        return {"success": True, "message": f"Leave application marked as {status} successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

# ==========================================
# DEFAULTERS LIST API
# ==========================================

@app.get("/api/defaulters/{user_id}")
def get_defaulters_list(user_id: str, month: str = "July", year: int = 2026, threshold: float = 75.0):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)
        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        sub_map = {s[1]: s[0] for s in sub_rows}

        sub_counts = conn.execute(text(f"""
            SELECT subject_id, COUNT(DISTINCT date) 
            FROM {t_attendance} 
            WHERE date LIKE :d 
            GROUP BY subject_id
        """), {"d": date_pattern}).fetchall()
        sub_total_classes = {r[0]: r[1] for r in sub_counts}

        att_rows = conn.execute(text(f"""
            SELECT student_id, COUNT(*) FROM {t_attendance} 
            WHERE status='Present' AND date LIKE :d 
            GROUP BY student_id
        """), {"d": date_pattern}).fetchall()
        present_map = {r[0]: r[1] for r in att_rows}

        tot_conducted = sum(sub_total_classes.values())

        defaulters = []
        for st in students:
            st_id, reg, roll, name = st
            tot_p = present_map.get(st_id, 0)
            pct = round((tot_p / tot_conducted * 100), 1) if tot_conducted > 0 else 0
            if pct < threshold:
                defaulters.append({
                    "reg_no": reg,
                    "roll_no": roll,
                    "name": name,
                    "attended": tot_p,
                    "total_classes": tot_conducted,
                    "overall_pct": pct,
                    "shortage": round(threshold - pct, 1)
                })

    return {"threshold": threshold, "total_classes": tot_conducted, "defaulters": defaulters}

# ==========================================
# FACULTY CORE API
# ==========================================

@app.get("/api/data/{user_id}")
def get_dashboard_data(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE", target_date: str = "2026-07-25"):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    t_settings = f"{safe_uid}_settings"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        total_students = conn.execute(text(f"SELECT COUNT(id) FROM {t_students}")).fetchone()[0] or 0
        sub_rows = conn.execute(text(f"SELECT subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        subjects = [r[0] for r in sub_rows]

        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None

        tc_count = 0
        present_today = 0
        if sub_id:
            tc_count = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            present_today = conn.execute(text(f"SELECT COUNT(date) FROM {t_attendance} WHERE subject_id=:sid AND date=:dt AND status='Present'"), {"sid": sub_id, "dt": target_date}).fetchone()[0] or 0

        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)
        st_list = [{"id": s[0], "reg_no": s[1], "roll_no": s[2], "name": s[3]} for s in students]

        def get_cfg(k, def_v):
            res = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key=:k"), {"k": k}).fetchone()
            return res[0] if res and res[0] else def_v

        c_name = get_cfg('college_name', 'INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)')
        c_sub = get_cfg('app_subtitle', 'ATTENDANCE MANAGEMENT SYSTEM')
        c_course = get_cfg('course_name', 'BCA')
        c_sec = get_cfg('section_name', 'Semester 1')
        logo_url = get_cfg('college_logo', 'https://i.ibb.co/3s68K1v/tree-logo.png')

    return {
        "total_students": total_students,
        "subjects": subjects,
        "classes_conducted": tc_count,
        "present_today": present_today,
        "students": st_list,
        "college_name": c_name,
        "app_subtitle": c_sub,
        "course_name": c_course,
        "section_name": c_sec,
        "college_logo": logo_url
    }

@app.get("/api/attendance_table/{user_id}")
def get_attendance_table(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE"):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"
    num_days = calendar.monthrange(year, month_num)[1]

    with engine.begin() as conn:
        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None

        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)

        att_map = {}
        tc_count = 0
        if sub_id:
            tc_count = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            records = conn.execute(text(f"""
                SELECT s.reg_no, a.date, a.status FROM {t_attendance} a 
                JOIN {t_students} s ON a.student_id = s.id 
                WHERE a.subject_id = :sid AND a.date LIKE :d
            """), {"sid": sub_id, "d": date_pattern}).fetchall()

            for r_no, d_str, stat in records:
                try:
                    day_idx = int(d_str.split('-')[2])
                    if r_no not in att_map: att_map[r_no] = {}
                    att_map[r_no][day_idx] = 'P' if stat == 'Present' else 'A'
                except: pass

        result = []
        for s in students:
            s_id, reg, roll, name = s
            days_data = {}
            total_p = 0
            for d in range(1, num_days + 1):
                val = att_map.get(reg, {}).get(d, "")
                days_data[d] = val
                if val == 'P': total_p += 1
            pct = round((total_p / tc_count * 100)) if tc_count > 0 else 0
            result.append({"id": s_id, "reg_no": reg, "roll_no": roll, "name": name, "days": days_data, "pct": pct})

    return {"num_days": num_days, "table_data": result, "total_classes": tc_count}

@app.get("/api/download_table_excel/{user_id}")
def download_table_excel(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE"):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"
    num_days = calendar.monthrange(year, month_num)[1]

    with engine.begin() as conn:
        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None

        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)

        att_map = {}
        tc_count = 0
        if sub_id:
            tc_count = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            records = conn.execute(text(f"""
                SELECT s.reg_no, a.date, a.status FROM {t_attendance} a 
                JOIN {t_students} s ON a.student_id = s.id 
                WHERE a.subject_id = :sid AND a.date LIKE :d
            """), {"sid": sub_id, "d": date_pattern}).fetchall()

            for r_no, d_str, stat in records:
                try:
                    day_idx = int(d_str.split('-')[2])
                    if r_no not in att_map: att_map[r_no] = {}
                    att_map[r_no][day_idx] = 'P' if stat == 'Present' else 'A'
                except: pass

        data = []
        for s in students:
            s_id, reg, roll, name = s
            row = {"Registration No": reg, "Roll No": roll, "Student Name": name}
            total_p = 0
            for d in range(1, num_days + 1):
                val = att_map.get(reg, {}).get(d, "")
                row[str(d)] = val
                if val == 'P': total_p += 1
            pct = round((total_p / tc_count * 100)) if tc_count > 0 else 0
            row["Overall %"] = f"{pct}%"
            data.append(row)

    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=f"{subject}_{month}")
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=Daily_Table_{subject}_{month}_{year}.xlsx"})

@app.get("/api/compile_report/{user_id}")
def get_compile_report(user_id: str, month: str = "July", year: int = 2026):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)
        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()

        sub_map = {s[1]: s[0] for s in sub_rows}
        subjects = list(sub_map.keys())

        sub_counts = conn.execute(text(f"""
            SELECT subject_id, COUNT(DISTINCT date) 
            FROM {t_attendance} 
            WHERE date LIKE :d 
            GROUP BY subject_id
        """), {"d": date_pattern}).fetchall()
        sub_total_classes = {r[0]: r[1] for r in sub_counts}

        att_rows = conn.execute(text(f"""
            SELECT student_id, subject_id, COUNT(*) FROM {t_attendance} 
            WHERE status='Present' AND date LIKE :d 
            GROUP BY student_id, subject_id
        """), {"d": date_pattern}).fetchall()

        present_map = {(r[0], r[1]): r[2] for r in att_rows}

        report = []
        for st in students:
            st_id, reg, roll, name = st
            row = {"reg_no": reg, "roll_no": roll, "name": name, "subs": {}}
            tot_p_all = 0
            tot_c_all = 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub_id, 0)
                tot_p = present_map.get((st_id, sub_id), 0)
                tot_p_all += tot_p
                tot_c_all += tot_c
                pct = round((tot_p / tot_c * 100)) if tot_c > 0 else 0
                row["subs"][sub] = f"{tot_p}/{tot_c} ({pct}%)"

            overall_pct = round((tot_p_all / tot_c_all * 100), 1) if tot_c_all > 0 else 0
            row["overall"] = f"{overall_pct}%"
            report.append(row)

    return {"subjects": subjects, "report": report}

@app.get("/api/download_excel/{user_id}")
def download_excel(user_id: str, month: str = "July", year: int = 2026):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)
        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        sub_map = {s[1]: s[0] for s in sub_rows}

        sub_counts = conn.execute(text(f"SELECT subject_id, COUNT(DISTINCT date) FROM {t_attendance} WHERE date LIKE :d GROUP BY subject_id"), {"d": date_pattern}).fetchall()
        sub_total_classes = {r[0]: r[1] for r in sub_counts}

        att_rows = conn.execute(text(f"SELECT student_id, subject_id, COUNT(*) FROM {t_attendance} WHERE status='Present' AND date LIKE :d GROUP BY student_id, subject_id"), {"d": date_pattern}).fetchall()
        present_map = {(r[0], r[1]): r[2] for r in att_rows}

        data = []
        for st in students:
            st_id, reg, roll, name = st
            row = {"Registration No": reg, "Roll No": roll, "Student Name": name}
            tot_p_all, tot_c_all = 0, 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub_id, 0)
                tot_p = present_map.get((st_id, sub_id), 0)
                tot_p_all += tot_p
                tot_c_all += tot_c
                pct = round((tot_p / tot_c * 100)) if tot_c > 0 else 0
                row[f"{sub} (P/T)"] = f"{tot_p}/{tot_c}"
                row[f"{sub} (%)"] = f"{pct}%"
            overall_pct = round((tot_p_all / tot_c_all * 100), 1) if tot_c_all > 0 else 0
            row["Overall %"] = f"{overall_pct}%"
            data.append(row)

    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=f"{month}_{year}")
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=Attendance_Report_{month}_{year}.xlsx"})

@app.get("/api/download_pdf/{user_id}")
def download_pdf(user_id: str, month: str = "July", year: int = 2026):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.platypus import SimpleDocTemplate, Table as RLTable, TableStyle, Paragraph, Spacer
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        raise HTTPException(status_code=500, detail="ReportLab not installed")

    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    t_settings = f"{safe_uid}_settings"
    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)
        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        sub_map = {s[1]: s[0] for s in sub_rows}
        subjects = list(sub_map.keys())

        sub_counts = conn.execute(text(f"SELECT subject_id, COUNT(DISTINCT date) FROM {t_attendance} WHERE date LIKE :d GROUP BY subject_id"), {"d": date_pattern}).fetchall()
        sub_total_classes = {r[0]: r[1] for r in sub_counts}

        att_rows = conn.execute(text(f"SELECT student_id, subject_id, COUNT(*) FROM {t_attendance} WHERE status='Present' AND date LIKE :d GROUP BY student_id, subject_id"), {"d": date_pattern}).fetchall()
        present_map = {(r[0], r[1]): r[2] for r in att_rows}

        c_name = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key='college_name'")).fetchone()
        college_name = c_name[0] if c_name else "INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)"

        headers = ["Roll No", "Reg No", "Name"] + subjects + ["Overall %"]
        table_data = [headers]

        for st in students:
            st_id, reg, roll, name = st
            row = [str(roll), str(reg), str(name)]
            tot_p_all, tot_c_all = 0, 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub_id, 0)
                tot_p = present_map.get((st_id, sub_id), 0)
                tot_p_all += tot_p
                tot_c_all += tot_c
                pct = round((tot_p / tot_c * 100)) if tot_c > 0 else 0
                row.append(f"{tot_p}/{tot_c} ({pct}%)")
            overall_pct = round((tot_p_all / tot_c_all * 100), 1) if tot_c_all > 0 else 0
            row.append(f"{overall_pct}%")
            table_data.append(row)

    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=landscape(A4), rightMargin=15, leftMargin=15, topMargin=15, bottomMargin=15)
    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('HeaderTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor('#0f172a'), alignment=1)
    sub_style = ParagraphStyle('HeaderSub', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#d97706'), alignment=1)

    elements.append(Paragraph(college_name, title_style))
    elements.append(Paragraph(f"CONSOLIDATED ATTENDANCE REPORT — {month.upper()} {year}", sub_style))
    elements.append(Spacer(1, 10))

    col_count = len(headers)
    col_w = max(40, 790 / col_count)
    t = RLTable(table_data, colWidths=[col_w]*col_count, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a8a')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f9ff')])
    ]))
    elements.append(t)
    doc.build(elements)
    pdf_buf.seek(0)
    return StreamingResponse(pdf_buf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Attendance_Report_{month}_{year}.pdf"})

@app.post("/api/mark_attendance")
def mark_attendance(user_id: str = Form(...), student_id: int = Form(...), subject: str = Form(...), date_str: str = Form(...), status: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_subjects = f"{safe_uid}_subjects"
        t_attendance = f"{safe_uid}_attendance"

        with engine.begin() as conn:
            sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
            if not sub_id_res: raise HTTPException(status_code=400, detail="Subject not found.")
            sub_id = sub_id_res[0]

            if status == 'Clear':
                conn.execute(text(f"DELETE FROM {t_attendance} WHERE student_id=:sid AND subject_id=:subid AND date=:dt"), 
                             {"sid": student_id, "subid": sub_id, "dt": date_str})
            else:
                conn.execute(text(f"""
                    INSERT INTO {t_attendance} (student_id, subject_id, date, status) 
                    VALUES (:sid, :subid, :dt, :stat) 
                    ON CONFLICT (student_id, subject_id, date) 
                    DO UPDATE SET status = :stat
                """), {"sid": student_id, "subid": sub_id, "dt": date_str, "stat": status})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/reset_attendance")
def reset_attendance(user_id: str = Form(...), scope: str = Form(...), reg_no: str = Form(None), subject: str = Form("All Subjects"), date_str: str = Form(None)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_students = f"{safe_uid}_students"
        t_subjects = f"{safe_uid}_subjects"
        t_attendance = f"{safe_uid}_attendance"

        with engine.begin() as conn:
            if scope == "single" and reg_no:
                s_res = conn.execute(text(f"SELECT id FROM {t_students} WHERE reg_no=:r"), {"r": reg_no}).fetchone()
                if s_res:
                    s_id = s_res[0]
                    if subject == "All Subjects":
                        conn.execute(text(f"DELETE FROM {t_attendance} WHERE student_id=:sid"), {"sid": s_id})
                    else:
                        sub_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
                        if sub_res:
                            conn.execute(text(f"DELETE FROM {t_attendance} WHERE student_id=:sid AND subject_id=:subid"), {"sid": s_id, "subid": sub_res[0]})
            elif scope == "class":
                if subject == "All Subjects":
                    conn.execute(text(f"DELETE FROM {t_attendance}"))
                else:
                    sub_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
                    if sub_res:
                        conn.execute(text(f"DELETE FROM {t_attendance} WHERE subject_id=:subid"), {"subid": sub_res[0]})
            elif scope == "date" and date_str:
                if subject == "All Subjects":
                    conn.execute(text(f"DELETE FROM {t_attendance} WHERE date=:dt"), {"dt": date_str})
                else:
                    sub_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
                    if sub_res:
                        conn.execute(text(f"DELETE FROM {t_attendance} WHERE date=:dt AND subject_id=:subid"), {"dt": date_str, "subid": sub_res[0]})
        return {"success": True, "message": "Attendance logs reset executed successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/delete_all_students")
def delete_all_students(user_id: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {safe_uid}_attendance"))
            conn.execute(text(f"DELETE FROM {safe_uid}_student_details"))
            conn.execute(text(f"DELETE FROM {safe_uid}_leaves"))
            conn.execute(text(f"DELETE FROM {safe_uid}_students"))
        return {"success": True, "message": "All students and their records deleted successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.get("/api/student_details/{user_id}/{reg_no:path}")
def get_student_profile(user_id: str, reg_no: str):
    safe_uid = get_safe_prefix(user_id)
    t_details = f"{safe_uid}_student_details"

    with engine.begin() as conn:
        res = conn.execute(text(f"SELECT email, contact, parent_name, parent_contact, res_type, photo_data FROM {t_details} WHERE reg_no=:r"), {"r": reg_no}).fetchone()

    if res:
        return {
            "email": res[0] or f"student@{reg_no.lower()}.ism.ac.in",
            "contact": res[1] or "+91 85000 00000",
            "parent_name": res[2] or "Parent / Guardian",
            "parent_contact": res[3] or "+91 98000 00000",
            "res_type": res[4] or "🏠 HOSTELER (Hostel Resident)",
            "photo_data": res[5] or "https://cdn-icons-png.flaticon.com/512/3135/3135715.png"
        }
    return {
        "email": f"student@{reg_no.lower()}.ism.ac.in",
        "contact": "+91 85000 00000",
        "parent_name": "Parent / Guardian",
        "parent_contact": "+91 98000 00000",
        "res_type": "🏠 HOSTELER (Hostel Resident)",
        "photo_data": "https://cdn-icons-png.flaticon.com/512/3135/3135715.png"
    }

@app.post("/api/add_student")
def add_student(user_id: str = Form(...), reg_no: str = Form(...), roll_no: str = Form(...), name: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_students = f"{safe_uid}_students"
        reg_clean = reg_no.strip()

        with engine.begin() as conn:
            faculties = conn.execute(text("SELECT username FROM master_users")).fetchall()
            for fac in faculties:
                f_id = fac[0]
                if f_id == user_id:
                    continue
                other_t = f"{get_safe_prefix(f_id)}_students"
                try:
                    exists = conn.execute(text(f"SELECT 1 FROM {other_t} WHERE LOWER(reg_no)=LOWER(:r)"), {"r": reg_clean}).fetchone()
                    if exists:
                        raise HTTPException(status_code=400, detail="This Registration Number is already registered in another class.")
                except HTTPException:
                    raise
                except Exception:
                    pass

            conn.execute(text(f"INSERT INTO {t_students} (reg_no, roll_no, name) VALUES (:r, :ro, :n) ON CONFLICT (reg_no) DO NOTHING"), {"r": reg_clean, "ro": roll_no.strip(), "n": name.strip()})
        return {"success": True, "message": "Student added successfully!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/delete_student")
def delete_student(user_id: str = Form(...), reg_no: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_students = f"{safe_uid}_students"
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {t_students} WHERE reg_no=:r"), {"r": reg_no.strip()})
        return {"success": True, "message": "Student deleted successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/import_students")
async def import_students(user_id: str = Form(...), file: UploadFile = File(...)):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    contents = await file.read()

    try:
        if file.filename.endswith('.csv'): 
            df_raw = pd.read_csv(io.BytesIO(contents))
        else: 
            df_raw = pd.read_excel(io.BytesIO(contents))

        def clean_col(c):
            return re.sub(r'[^a-zA-Z0-9]', '', str(c).lower())

        cleaned_cols = {col: clean_col(col) for col in df_raw.columns}
        cols = list(df_raw.columns)

        mapped_reg, mapped_roll, mapped_name = None, None, None

        for orig_col, clean_name in cleaned_cols.items():
            if not mapped_reg and ('reg' in clean_name or 'enrol' in clean_name or 'id' in clean_name):
                mapped_reg = orig_col
            elif not mapped_roll and ('roll' in clean_name or 'sl' in clean_name or 'sr' in clean_name or 'sn' in clean_name or 'serial' in clean_name):
                mapped_roll = orig_col
            elif not mapped_name and ('name' in clean_name or 'student' in clean_name):
                mapped_name = orig_col

        if not mapped_reg and len(cols) > 0: mapped_reg = cols[0]
        if not mapped_roll and len(cols) > 1: mapped_roll = cols[1]
        if not mapped_name and len(cols) > 2: mapped_name = cols[2]

        if not mapped_reg or not mapped_name:
            raise HTTPException(status_code=400, detail="Error: Could not find Reg No and Name columns in file.")

        inserted_students = 0
        skipped_list = []

        with engine.begin() as conn:
            other_regs = set()
            faculties = conn.execute(text("SELECT username FROM master_users")).fetchall()
            for fac in faculties:
                f_id = fac[0]
                if f_id == user_id:
                    continue
                other_t = f"{get_safe_prefix(f_id)}_students"
                try:
                    res = conn.execute(text(f"SELECT reg_no FROM {other_t}")).fetchall()
                    for r in res:
                        other_regs.add(str(r[0]).strip().lower())
                except:
                    pass

            for _, row in df_raw.iterrows():
                reg = str(row.get(mapped_reg, "")).strip()
                if reg.endswith('.0'): reg = reg[:-2]
                if reg.lower() == 'nan': reg = ""

                roll = str(row.get(mapped_roll, "")) if mapped_roll else ""
                roll = roll.strip()
                if roll.endswith('.0'): roll = roll[:-2]
                if roll.lower() == 'nan': roll = ""

                name = str(row.get(mapped_name, "")).strip()
                if name.lower() == 'nan': name = ""

                if reg and name:
                    if reg.lower() in other_regs:
                        skipped_list.append(f"{reg} - {name}")
                        continue

                    conn.execute(text(f"""
                        INSERT INTO {t_students} (reg_no, roll_no, name) 
                        VALUES (:r, :ro, :n) 
                        ON CONFLICT (reg_no) 
                        DO UPDATE SET roll_no = EXCLUDED.roll_no, name = EXCLUDED.name
                    """), {"r": reg, "ro": roll, "n": name})
                    inserted_students += 1

        return {"success": True, "message": f"Successfully registered {inserted_students} students.", "skipped": skipped_list}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Import Failed: " + str(e))

@app.post("/api/import_attendance")
async def import_attendance(user_id: str = Form(...), file: UploadFile = File(...), subject: str = Form(...), date_str: str = Form(...)):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    contents = await file.read()

    try:
        if file.filename.endswith('.csv'): 
            df_raw = pd.read_csv(io.BytesIO(contents))
        else: 
            df_raw = pd.read_excel(io.BytesIO(contents))

        def clean_col(c):
            return re.sub(r'[^a-zA-Z0-9]', '', str(c).lower())

        cleaned_cols = {col: clean_col(col) for col in df_raw.columns}
        cols = list(df_raw.columns)

        mapped_reg, mapped_name, mapped_att = None, None, None

        for orig_col, clean_name in cleaned_cols.items():
            if not mapped_reg and ('reg' in clean_name or 'enrol' in clean_name or 'id' in clean_name):
                mapped_reg = orig_col
            elif not mapped_name and ('name' in clean_name or 'student' in clean_name):
                mapped_name = orig_col

        try:
            target_day = str(int(date_str.split('-')[2]))
            if target_day in cols:
                mapped_att = target_day
            elif int(target_day) in cols:
                mapped_att = int(target_day)
        except:
            pass

        if not mapped_att:
            for orig_col, clean_name in cleaned_cols.items():
                if ('att' in clean_name or 'stat' in clean_name or 'pa' in clean_name or 'mark' in clean_name or 'present' in clean_name):
                    mapped_att = orig_col
                    break

        if not mapped_reg and len(cols) > 0: mapped_reg = cols[0]

        if not mapped_att and len(cols) > 1: 
            if cols[-1] == 'Overall %' and len(cols) > 2:
                mapped_att = cols[-2]
            else:
                mapped_att = cols[-1]

        if not mapped_reg or not mapped_att:
            raise HTTPException(status_code=400, detail="Error: File must contain Reg No and Attendance Status columns.")

        inserted_att = 0
        with engine.begin() as conn:
            sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
            if not sub_id_res:
                 raise HTTPException(status_code=400, detail="Error: Selected Subject not found in database.")
            sub_id = sub_id_res[0]

            for _, row in df_raw.iterrows():
                reg = str(row.get(mapped_reg, "")).strip()
                if reg.endswith('.0'): reg = reg[:-2]
                if reg.lower() == 'nan': reg = ""

                if reg:
                    att_val = str(row.get(mapped_att, "")).strip().lower()
                    status = ""
                    if att_val in ['p', 'present', '1', 'yes', 'true', 'y']:
                        status = 'Present'
                    elif att_val in ['a', 'absent', '0', 'no', 'false', 'n']:
                        status = 'Absent'

                    if status:
                        s_res = conn.execute(text(f"SELECT id FROM {t_students} WHERE reg_no=:r"), {"r": reg}).fetchone()
                        if s_res:
                            student_id = s_res[0]
                            conn.execute(text(f"""
                                INSERT INTO {t_attendance} (student_id, subject_id, date, status) 
                                VALUES (:sid, :subid, :dt, :stat) 
                                ON CONFLICT (student_id, subject_id, date) 
                                DO UPDATE SET status = :stat
                            """), {"sid": student_id, "subid": sub_id, "dt": date_str, "stat": status})
                            inserted_att += 1

        return {"success": True, "message": f"Successfully marked attendance for {inserted_att} students on {date_str} for subject {subject}."}

    except Exception as e:
        raise HTTPException(status_code=400, detail="Attendance Import Failed: " + str(e))

@app.post("/api/add_subject")
def add_subject(user_id: str = Form(...), subject_name: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_subjects = f"{safe_uid}_subjects"
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {t_subjects} (subject_name) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": subject_name.strip()})
        return {"success": True, "message": "Subject added successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/delete_subject")
def delete_subject(user_id: str = Form(...), subject_name: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_subjects = f"{safe_uid}_subjects"
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {t_subjects} WHERE subject_name=:s"), {"s": subject_name.strip()})
        return {"success": True, "message": "Subject deleted successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/upload_logo")
async def upload_logo(user_id: str = Form(...), file: UploadFile = File(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_settings = f"{safe_uid}_settings"
        contents = await file.read()
        ext = file.filename.split('.')[-1].lower()
        b64_val = f"data:image/{ext};base64,{base64.b64encode(contents).decode('utf-8')}"
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('college_logo', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": b64_val})
        return {"success": True, "logo_url": b64_val, "message": "Logo uploaded successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/save_college_profile")
def save_college_profile(user_id: str = Form(...), college_name: str = Form(...), subtitle: str = Form(...), course_name: str = Form(...), section_name: str = Form(...)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_settings = f"{safe_uid}_settings"
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('college_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": college_name})
            conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('app_subtitle', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": subtitle})
            conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('course_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": course_name})
            conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('section_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": section_name})
        return {"success": True, "message": "College profile updated successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

@app.post("/api/save_student_profile")
async def save_student_profile(user_id: str = Form(...), reg_no: str = Form(...), email: str = Form(...), contact: str = Form(...), parent_name: str = Form(...), parent_contact: str = Form(...), res_type: str = Form(...), file: UploadFile = File(None)):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_details = f"{safe_uid}_student_details"
        encoded_img = None
        if file and file.filename:
            contents = await file.read()
            ext = file.filename.split('.')[-1].lower()
            encoded_img = f"data:image/{ext};base64,{base64.b64encode(contents).decode('utf-8')}"
        with engine.begin() as conn:
            if encoded_img:
                conn.execute(text(f"""
                    INSERT INTO {t_details} (reg_no, email, contact, parent_name, parent_contact, res_type, photo_data) 
                    VALUES (:r, :e, :c, :pn, :pc, :rt, :pd) 
                    ON CONFLICT (reg_no) 
                    DO UPDATE SET email = EXCLUDED.email, contact = EXCLUDED.contact, parent_name = EXCLUDED.parent_name, parent_contact = EXCLUDED.parent_contact, res_type = EXCLUDED.res_type, photo_data = EXCLUDED.photo_data
                """), {"r": reg_no.strip(), "e": email, "c": contact, "pn": parent_name, "pc": parent_contact, "rt": res_type, "pd": encoded_img})
            else:
                conn.execute(text(f"""
                    INSERT INTO {t_details} (reg_no, email, contact, parent_name, parent_contact, res_type) 
                    VALUES (:r, :e, :c, :pn, :pc, :rt) 
                    ON CONFLICT (reg_no) 
                    DO UPDATE SET email = EXCLUDED.email, contact = EXCLUDED.contact, parent_name = EXCLUDED.parent_name, parent_contact = EXCLUDED.parent_contact, res_type = EXCLUDED.res_type
                """), {"r": reg_no.strip(), "e": email, "c": contact, "pn": parent_name, "pc": parent_contact, "rt": res_type})
        return {"success": True, "message": "Student profile saved successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

# ==========================================
# FULL FRONTEND (HTML + JAVASCRIPT)
# ==========================================

@app.get("/", response_class=HTMLResponse)
def home():
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ISM Attendance ERP - Final Full Edition</title>
    <script src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" defer></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: radial-gradient(circle at 50% 30%, #1e3a8a 0%, #0f172a 60%, #020617 100%); min-height: 100vh; color: white; overflow-x: hidden; position: relative; }
        .anim-container { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; pointer-events: none; overflow: hidden; z-index: 0; }
        .floating-icon { position: absolute; bottom: -150px; opacity: 0.45; animation: floatUp 15s infinite linear; filter: drop-shadow(0 0 15px rgba(56, 189, 248, 0.9)); }
        @keyframes floatUp { 0% { transform: translateY(0) rotate(0deg) scale(0.9); opacity: 0; } 20% { opacity: 0.5; } 80% { opacity: 0.5; } 100% { transform: translateY(-115vh) rotate(360deg) scale(1.2); opacity: 0; } }
        .glass-card { background: rgba(15, 23, 42, 0.85) !important; backdrop-filter: blur(10px); border: 2px solid rgba(56, 189, 248, 0.3); }
        input, select, textarea { background-color: #e0f2fe !important; color: #0f172a !important; border: 2px solid #38bdf8 !important; font-weight: 800 !important; }
        input::placeholder, textarea::placeholder { color: #64748b !important; }
        .math-grid-table th, .math-grid-table td { border: 2px solid #38bdf8 !important; }
        .math-grid-table th:nth-child(3), .math-grid-table td:nth-child(3) { min-width: 320px !important; text-align: left !important; padding-left: 14px !important; }
        .math-grid-table td:not(:nth-child(3)):not(:nth-child(1)):not(:nth-child(2)) { width: 34px !important; height: 34px !important; min-width: 34px !important; max-width: 34px !important; padding: 2px !important; font-size: 11px !important; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #38bdf8; border-radius: 4px; }
    </style>
</head>
<body x-data="erpApp()">
    <div class="anim-container">
        <div class="floating-icon" style="left: 5%; animation-delay: 0s; font-size: 70px;">📅</div>
        <div class="floating-icon" style="left: 20%; animation-delay: 4s; font-size: 80px;">✅</div>
        <div class="floating-icon" style="left: 35%; animation-delay: 2s; font-size: 85px;">📊</div>
        <div class="floating-icon" style="left: 50%; animation-delay: 7s; font-size: 75px;">⏰</div>
        <div class="floating-icon" style="left: 65%; animation-delay: 1s; font-size: 90px;">🎓</div>
        <div class="floating-icon" style="left: 80%; animation-delay: 5s; font-size: 80px;">📈</div>
        <div class="floating-icon" style="left: 90%; animation-delay: 3s; font-size: 75px;">⭐</div>
    </div>

    <!-- MAIN LOGIN SCREEN -->
    <div x-show="!loggedIn" class="flex items-center justify-center min-h-screen p-6 relative z-10">
        <div class="glass-card p-10 rounded-3xl shadow-2xl w-full max-w-5xl grid grid-cols-1 md:grid-cols-2 gap-10 items-center">
            <!-- Left Branding Side -->
            <div>
                <div class="inline-block bg-sky-950/80 border border-sky-400/40 px-3 py-1 rounded-full text-xs font-bold text-sky-400 mb-4 shadow">⚡ ENTERPRISE CLOUD PORTAL</div>
                <h1 class="text-4xl font-black text-white mb-2">🎓 ISM PATNA</h1>
                <h3 class="text-lg font-bold text-amber-400 mb-4">ATTENDANCE ERP SYSTEM</h3>
                <p class="text-slate-300 text-sm leading-relaxed mb-6">Welcome to the professional Multi-Tenant Attendance ERP Platform. Select your portal to proceed securely.</p>

                <div class="space-y-4">
                    <div class="bg-sky-950/60 border border-sky-500/40 p-4 rounded-xl flex items-center gap-4">
                        <div class="text-3xl">👨‍🏫</div>
                        <div>
                            <p class="text-sky-300 font-bold text-sm">Faculty Login</p>
                            <p class="text-slate-400 text-xs">For Teachers and Admins to mark attendance, manage leaves and records.</p>
                        </div>
                    </div>
                    <div class="bg-emerald-950/60 border border-emerald-500/40 p-4 rounded-xl flex items-center gap-4">
                        <div class="text-3xl">🎓</div>
                        <div>
                            <p class="text-emerald-300 font-bold text-sm">Student Portal</p>
                            <p class="text-slate-400 text-xs">Track attendance records, check status & submit leaves.</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Right Login Form Side -->
            <div class="bg-slate-900/90 p-8 rounded-2xl border border-sky-400/30 shadow-2xl">
                <!-- Role Selector Tabs -->
                <div class="flex gap-2 mb-6 bg-slate-950 p-1.5 rounded-xl border border-slate-700">
                    <button @click="authRole = 'faculty'; isLogin = true" :class="authRole === 'faculty' ? 'bg-blue-600 text-white shadow' : 'text-slate-400'" class="flex-1 py-3 font-black rounded-lg transition text-sm">👨‍🏫 FACULTY</button>
                    <button @click="authRole = 'student'" :class="authRole === 'student' ? 'bg-emerald-600 text-white shadow' : 'text-slate-400'" class="flex-1 py-3 font-black rounded-lg transition text-sm">🎓 STUDENT</button>
                </div>

                <!-- FACULTY LOGIN / REGISTER FORM -->
                <div x-show="authRole === 'faculty'">
                    <div class="flex gap-2 mb-6 bg-slate-800 p-1 rounded-xl">
                        <button @click="isLogin = true" :class="isLogin ? 'bg-sky-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-1.5 font-bold rounded-lg transition text-xs">🔐 Login</button>
                        <button @click="isLogin = false" :class="!isLogin ? 'bg-sky-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-1.5 font-bold rounded-lg transition text-xs">📄 Register New Class</button>
                    </div>
                    <form @submit.prevent="submitAuth" class="space-y-4">
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Faculty ID / Class Code</label>
                            <input type="text" x-model="authForm.username" placeholder="Enter Faculty ID" required class="w-full p-3 rounded-xl text-sm">
                        </div>
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Password</label>
                            <input type="password" x-model="authForm.password" placeholder="Enter Password" required class="w-full p-3 rounded-xl text-sm">
                        </div>
                        <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-black py-3 rounded-xl shadow-lg transition text-sm" x-text="isLogin ? 'FACULTY LOGIN' : 'CREATE CLASS PORTAL'"></button>
                    </form>
                </div>

                <!-- STUDENT LOGIN FORM -->
                <div x-show="authRole === 'student'">
                    <p class="text-emerald-400 text-xs font-bold mb-4 text-center">Secure Student Portal Access</p>
                    <form @submit.prevent="submitStudentAuth" class="space-y-4">
                        <div>
                            <label class="block text-emerald-400 font-bold text-xs mb-1">Registration No.</label>
                            <input type="text" x-model="studentForm.reg_no" placeholder="Enter your Reg No." required class="w-full p-3 rounded-xl text-sm border-emerald-400 focus:border-emerald-500">
                        </div>
                        <div>
                            <label class="block text-emerald-400 font-bold text-xs mb-1">Student Full Name</label>
                            <input type="text" x-model="studentForm.name" placeholder="Enter your full name as registered" required class="w-full p-3 rounded-xl text-sm border-emerald-400 focus:border-emerald-500">
                        </div>
                        <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl shadow-lg transition text-sm">ACCESS STUDENT PORTAL</button>
                    </form>
                </div>

                <p x-text="authError" class="text-red-400 text-center text-xs font-bold mt-4"></p>
            </div>
        </div>
    </div>

    <!-- ============================================================== -->
    <!-- FACULTY DASHBOARD -->
    <!-- ============================================================== -->
    <div x-show="loggedIn && userRole === 'faculty'" class="flex h-screen overflow-hidden relative z-10" style="display: none;">
        <!-- Left Sidebar Navigation -->
        <div class="w-72 bg-gradient-to-b from-blue-950 via-slate-950 to-slate-950 border-r-2 border-sky-400/50 flex flex-col justify-between p-4 shadow-2xl relative overflow-hidden">
            <div class="relative z-10">
                <div class="flex flex-col items-center mb-6">
                    <img :src="collegeLogo" class="w-20 h-20 rounded-full bg-white p-1 border-4 border-sky-400 shadow-lg mb-2 object-contain">
                    <span class="text-yellow-400 font-bold text-sm" x-text="'Faculty: ' + userId"></span>
                </div>
                <p class="text-slate-400 text-xs font-bold mb-2">Navigate Pages:</p>
                <nav class="space-y-2 text-sm font-black">
                    <button @click="currentTab = 'dashboard'; loadData()" :class="currentTab === 'dashboard' ? 'bg-blue-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-blue-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📊 Dashboard</button>
                    <button @click="currentTab = 'mark'; loadData()" :class="currentTab === 'mark' ? 'bg-emerald-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-emerald-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📝 Mark Attendance</button>
                    <button @click="currentTab = 'table'; syncToLive(); loadTableData()" :class="currentTab === 'table' ? 'bg-purple-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-purple-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📅 Attendance Table</button>
                    <button @click="currentTab = 'report'; syncToLive(); loadReportData()" :class="currentTab === 'report' ? 'bg-amber-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-amber-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📑 Monthly Compile Report</button>
                    <button @click="currentTab = 'reset'" :class="currentTab === 'reset' ? 'bg-red-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-red-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🧹 Reset / Clear Logs</button>
                    <button @click="currentTab = 'students'; loadData()" :class="currentTab === 'students' ? 'bg-cyan-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-cyan-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">👥 Manage Students</button>
                    
                    <button @click="currentTab = 'leaves'; loadFacultyLeaves()" :class="currentTab === 'leaves' ? 'bg-indigo-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-indigo-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center justify-between">
                        <span class="flex items-center gap-2">📩 Leave Requests</span>
                        <span x-show="pendingLeavesCount > 0" class="bg-amber-400 text-slate-900 px-2 py-0.5 rounded-full text-xs font-black" x-text="pendingLeavesCount"></span>
                    </button>

                    <button @click="currentTab = 'profile'" :class="currentTab === 'profile' ? 'bg-pink-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-pink-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🏢 College Profile</button>
                </nav>
            </div>
            <button @click="logout" class="bg-emerald-500 hover:bg-emerald-600 py-3 rounded-xl font-black text-center shadow-lg transition relative z-10">🚪 LOGOUT FROM PORTAL</button>
        </div>

        <!-- MAIN FACULTY CONTENT VIEW -->
        <div class="flex-1 flex flex-col overflow-y-auto p-6">
            <div class="glass-card p-4 rounded-2xl shadow-xl flex items-center gap-4 mb-6 border-b-4 border-amber-500">
                <img :src="collegeLogo" class="w-16 h-16 object-contain bg-white rounded-lg p-1">
                <div>
                    <h1 class="text-xl font-black text-white" x-text="collegeName"></h1>
                    <p class="text-yellow-400 font-bold text-xs mt-1" x-text="appSubtitle + ' | ' + courseName + ' | ' + sectionName"></p>
                </div>
            </div>

            <!-- TAB 1: DASHBOARD -->
            <div x-show="currentTab === 'dashboard'">
                <h2 class="text-xl font-black text-white mb-4 flex items-center gap-2">📊 Monthly Overview & Daily Status</h2>
                <div class="grid grid-cols-4 gap-4 mb-6">
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Month</label>
                        <select x-model="selectedMonth" @change="loadData()" class="w-full p-2.5 rounded-xl">
                            <template x-for="m in months"><option :value="m" :selected="m == selectedMonth" x-text="m"></option></template>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Year</label>
                        <select x-model="selectedYear" @change="loadData()" class="w-full p-2.5 rounded-xl">
                            <template x-for="y in years"><option :value="y" :selected="y == selectedYear" x-text="y"></option></template>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Subject</label>
                        <select x-model="selectedSubject" @change="loadData()" class="w-full p-2.5 rounded-xl">
                            <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Select Date</label>
                        <input type="date" x-model="selectedDate" @change="syncFromDate(); loadData()" class="w-full p-2.5 rounded-xl">
                    </div>
                </div>

                <div class="grid grid-cols-4 gap-6">
                    <div class="bg-white text-slate-900 p-6 rounded-2xl shadow-xl text-center border-t-8 border-blue-500">
                        <p class="font-bold text-slate-600">Total Students</p>
                        <h3 class="text-4xl font-black text-blue-900 mt-2" x-text="totalStudents"></h3>
                    </div>
                    <div class="bg-white text-slate-900 p-6 rounded-2xl shadow-xl text-center border-t-8 border-purple-500">
                        <p class="font-bold text-slate-600">Classes Conducted</p>
                        <h3 class="text-4xl font-black text-purple-900 mt-2" x-text="classesConducted"></h3>
                    </div>
                    <div class="bg-emerald-50 text-slate-900 p-6 rounded-2xl shadow-xl text-center border-t-8 border-emerald-500">
                        <p class="font-bold text-slate-600">Selected Subject</p>
                        <h3 class="text-3xl font-black text-emerald-900 mt-2" x-text="selectedSubject"></h3>
                    </div>
                    <div class="bg-amber-50 text-slate-900 p-6 rounded-2xl shadow-xl text-center border-t-8 border-amber-500">
                        <p class="font-bold text-slate-600" x-text="'Present on ' + selectedDate"></p>
                        <h3 class="text-4xl font-black text-amber-900 mt-2" x-text="presentToday"></h3>
                    </div>
                </div>

                <!-- Defaulters List Action Card -->
                <div class="mt-8 glass-card p-6 rounded-2xl border-2 border-red-500/50">
                    <div class="flex flex-col md:flex-row justify-between items-center gap-4">
                        <div>
                            <h3 class="text-xl font-black text-red-400 flex items-center gap-2">⚠️ Attendance Defaulters List (&lt; 75%)</h3>
                            <p class="text-xs text-slate-300 mt-1">Quickly scan and identify all students with attendance below standard criteria for <span class="text-amber-400 font-bold" x-text="selectedMonth + ' ' + selectedYear"></span>.</p>
                        </div>
                        <button @click="openDefaultersModal()" class="bg-red-600 hover:bg-red-700 text-white font-black py-3 px-6 rounded-xl shadow-xl transition flex items-center gap-2 transform active:scale-95">
                            🚨 VIEW DEFAULTERS LIST
                        </button>
                    </div>
                </div>
            </div>

            <!-- TAB 2: MARK ATTENDANCE -->
            <div x-show="currentTab === 'mark'">
                <div class="grid grid-cols-4 gap-4 mb-6">
                    <select x-model="selectedMonth" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months"><option :value="m" :selected="m == selectedMonth" x-text="m"></option></template>
                    </select>
                    <select x-model="selectedYear" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years"><option :value="y" :selected="y == selectedYear" x-text="y"></option></template>
                    </select>
                    <select x-model="selectedSubject" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                    </select>
                    <input type="date" x-model="selectedDate" @change="syncFromDate(); loadData()" class="w-full p-2.5 rounded-xl">
                </div>

                <div class="grid grid-cols-2 gap-8 items-start" x-show="students.length > 0">
                    <div class="bg-gradient-to-b from-[#fefdfa] to-[#f8f5e9] text-slate-900 p-6 rounded-3xl shadow-2xl border-4 border-slate-300 max-w-sm mx-auto w-full relative">
                        <div class="w-12 h-2 bg-slate-800 rounded-full mx-auto mb-4"></div>
                        <div class="bg-gradient-to-r from-emerald-700 to-emerald-900 text-white p-3 rounded-xl flex items-center gap-3 border-b-4 border-amber-400">
                            <img :src="collegeLogo" class="w-8 h-8 bg-white rounded-full p-0.5 object-contain">
                            <div class="text-[11px] font-black leading-tight" x-text="collegeName.toUpperCase()"></div>
                        </div>
                        <div class="flex justify-center my-4 relative">
                            <img :src="currentStudentPhoto" class="w-28 h-28 rounded-full object-cover border-4 border-sky-500 shadow-md bg-white">
                            <span class="absolute bottom-0 right-20 text-red-600 font-black text-xs bg-white px-1 rounded shadow">🩸 A+</span>
                        </div>
                        <h2 class="text-center text-xl font-black text-slate-900" x-text="currentStudent.name"></h2>
                        <p class="text-center font-bold text-slate-700 text-xs" x-text="'ROLL NO : ' + currentStudent.roll_no"></p>
                        <p class="text-center font-bold text-slate-600 text-xs mb-3" x-text="'REG NO : ' + currentStudent.reg_no"></p>
                        <div class="bg-emerald-700 text-white text-center font-black py-1 rounded-lg text-xs mb-3 shadow" x-text="courseName + ' - ' + sectionName"></div>
                        <div class="bg-sky-600 text-white text-center font-bold py-1 rounded-full text-[11px] mb-3 shadow" x-text="currentStudentDetails.res_type"></div>
                        <div class="text-[11px] space-y-1 font-semibold text-slate-800 border-t border-sky-400 border-dashed pt-2">
                            <p><b>Email :</b> <span x-text="currentStudentDetails.email"></span></p>
                            <p><b>Contact :</b> <span x-text="currentStudentDetails.contact"></span></p>
                            <p><b>Guardian :</b> <span x-text="currentStudentDetails.parent_name + ' (' + currentStudentDetails.parent_contact + ')'"></span></p>
                        </div>
                        <div class="flex justify-between items-end mt-4 pt-2 border-t border-sky-400 text-[10px] font-bold text-slate-600">
                            <span>Valid till : <b>2028</b></span>
                            <div class="text-center">
                                <p class="font-serif italic text-slate-900 text-xs">Principal</p>
                                <p class="border-t border-slate-500">Principal</p>
                            </div>
                        </div>
                    </div>

                    <div class="space-y-6">
                        <h3 class="text-xl font-black text-amber-400 flex items-center gap-2">⚡ Action Controls:</h3>
                        <div class="grid grid-cols-2 gap-4">
                            <button @click="markStatusBtn('Present')" class="bg-emerald-500 hover:bg-emerald-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition transform active:scale-95">🟢 MARK PRESENT (P)</button>
                            <button @click="markStatusBtn('Absent')" class="bg-red-500 hover:bg-red-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition transform active:scale-95">🔴 MARK ABSENT (A)</button>
                        </div>
                        <div>
                            <label class="block text-white font-bold text-sm mb-1">🔍 Search Student Directly by Reg No:</label>
                            <input type="text" x-model="searchReg" @input="searchByReg" placeholder="Type exact Registration Number here..." class="w-full p-3 rounded-xl shadow">
                        </div>
                        <div>
                            <label class="block text-white font-bold text-sm mb-1">🔍 Quick Jump to Student</label>
                            <select x-model="currentIndex" @change="fetchStudentDetails" class="w-full p-3 rounded-xl">
                                <template x-for="(st, idx) in students">
                                    <option :value="idx" x-text="st.reg_no + ' - ' + st.name"></option>
                                </template>
                            </select>
                        </div>
                        <div class="flex gap-4">
                            <button @click="prevStudent" class="flex-1 bg-red-600 hover:bg-red-700 py-3 rounded-xl font-black shadow">◀ PREVIOUS</button>
                            <button @click="nextStudent" class="flex-1 bg-red-600 hover:bg-red-700 py-3 rounded-xl font-black shadow">NEXT ▶</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TAB 3: ATTENDANCE TABLE -->
            <div x-show="currentTab === 'table'">
                <h2 class="text-2xl font-black text-white mb-4">📅 Monthly Register & Inline Editor</h2>
                <div class="grid grid-cols-3 gap-4 mb-4">
                    <select x-model="tableMonth" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months"><option :value="m" :selected="m == tableMonth" x-text="m"></option></template>
                    </select>
                    <select x-model="tableYear" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years"><option :value="y" :selected="y == tableYear" x-text="y"></option></template>
                    </select>
                    <select x-model="tableSubject" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                    </select>
                </div>

                <div class="flex gap-4 mb-6">
                    <a :href="'/api/download_table_excel/' + encodeURIComponent(userId) + '?month=' + tableMonth + '&year=' + tableYear + '&subject=' + encodeURIComponent(tableSubject)" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 px-6 rounded-xl text-center shadow-lg transition">📊 DOWNLOAD THIS TABLE TO EXCEL (.XLSX)</a>
                </div>

                <div class="mb-4">
                    <input type="text" x-model="tableSearchQuery" placeholder="🔍 Search specific student by Name, Reg No, or Roll No..." class="w-full p-3 rounded-xl shadow border-2 border-sky-400 bg-white text-slate-900 font-bold focus:ring-4 focus:ring-sky-500 transition">
                </div>

                <p class="text-sky-300 font-bold text-xs mb-2">💡 Tip: You can click directly on any box below to toggle Attendance (1 Click = Present, 2 Clicks = Absent, 3 Clicks = Clear).</p>

                <div class="bg-sky-100 rounded-xl overflow-x-auto border-2 border-sky-400 shadow-2xl">
                    <table class="w-full text-slate-900 font-bold text-sm text-center math-grid-table border-collapse">
                        <thead>
                            <tr class="bg-blue-900 text-white">
                                <th class="p-3 border sticky left-0 bg-blue-900 z-10 w-28">Reg No</th>
                                <th class="p-3 border sticky left-28 bg-blue-900 z-10 w-16">Roll</th>
                                <th class="p-3 border text-left sticky left-44 bg-blue-900 z-10 w-80">Student Name</th>
                                <template x-for="d in tableNumDays">
                                    <th class="p-1 border text-xs w-8 h-8" x-text="d"></th>
                                </template>
                                <th class="p-3 border w-16">%</th>
                            </tr>
                        </thead>
                        <tbody>
                            <template x-for="st in filteredTableRows">
                                <tr class="bg-sky-50 hover:bg-sky-200">
                                    <td class="p-3 border sticky left-0 bg-sky-50 z-10" x-text="st.reg_no"></td>
                                    <td class="p-3 border sticky left-28 bg-sky-50 z-10" x-text="st.roll_no"></td>
                                    <td class="p-3 border text-left sticky left-44 bg-sky-50 z-10 truncate" x-text="st.name"></td>
                                    <template x-for="d in tableNumDays">
                                        <td class="border text-xs text-center cursor-pointer transition-colors duration-200 select-none" 
                                            title="Click to toggle Present/Absent"
                                            :class="st.days[d] === 'P' ? 'bg-emerald-500 text-white font-black hover:bg-emerald-600' : (st.days[d] === 'A' ? 'bg-red-500 text-white font-black hover:bg-red-600' : 'hover:bg-sky-200')" 
                                            x-text="st.days[d]"
                                            @click="toggleCellAttendance(st, d)">
                                        </td>
                                    </template>
                                    <td class="p-3 border font-black text-blue-800" x-text="st.pct + '%'"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- TAB 4: COMPILE REPORT -->
            <div x-show="currentTab === 'report'">
                <h2 class="text-2xl font-black text-white mb-4">📑 Consolidated Monthly Attendance & Percentage Report</h2>
                <div class="grid grid-cols-2 gap-4 mb-6">
                    <select x-model="reportMonth" @change="loadReportData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months"><option :value="m" :selected="m == reportMonth" x-text="m"></option></template>
                    </select>
                    <select x-model="reportYear" @change="loadReportData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years"><option :value="y" :selected="y == reportYear" x-text="y"></option></template>
                    </select>
                </div>

                <div class="flex gap-4 mb-6">
                    <a :href="'/api/download_excel/' + encodeURIComponent(userId) + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition">📊 DOWNLOAD EXCEL (.XLSX)</a>
                    <a :href="'/api/download_pdf/' + encodeURIComponent(userId) + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-red-600 hover:bg-red-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition">📥 DOWNLOAD PDF (.PDF)</a>
                    <button @click="shareViaEmail()" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition flex justify-center items-center gap-2">🔗 SHARE PDF</button>
                </div>

                <div class="mb-6">
                    <input type="text" x-model="reportSearchQuery" placeholder="🔍 Search specific student by Name, Reg No, or Roll No..." class="w-full p-3 rounded-xl shadow border-2 border-sky-400 bg-white text-slate-900 font-bold focus:ring-4 focus:ring-sky-500 transition">
                </div>

                <div class="bg-sky-100 rounded-xl overflow-x-auto border-2 border-sky-400 shadow-2xl">
                    <table class="w-full text-slate-900 font-bold text-sm text-center">
                        <thead>
                            <tr class="bg-blue-900 text-white">
                                <th class="p-3 border">Reg No</th>
                                <th class="p-3 border">Roll No</th>
                                <th class="p-3 border text-left">Student Name</th>
                                <template x-for="sub in reportSubjects">
                                    <th class="p-2 border" x-text="sub"></th>
                                </template>
                                <th class="p-3 border">Overall %</th>
                            </tr>
                        </thead>
                        <tbody>
                            <template x-for="st in filteredReportRows">
                                <tr class="border-b bg-sky-50">
                                    <td class="p-3 border" x-text="st.reg_no"></td>
                                    <td class="p-3 border" x-text="st.roll_no"></td>
                                    <td class="p-3 border text-left" x-text="st.name"></td>
                                    <template x-for="sub in reportSubjects">
                                        <td class="p-2 border" x-text="st.subs[sub]"></td>
                                    </template>
                                    <td class="p-3 border font-black text-emerald-700" x-text="st.overall"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- TAB 5: RESET / CLEAR LOGS -->
            <div x-show="currentTab === 'reset'">
                <h2 class="text-2xl font-black text-white mb-4">🧹 Reset / Clear Attendance Logs</h2>
                <div class="glass-card p-6 rounded-2xl space-y-6">
                    <div>
                        <label class="block text-sky-400 font-bold mb-2">Select Reset Scope:</label>
                        <div class="grid grid-cols-3 gap-4">
                            <button @click="resetScope = 'single'" :class="resetScope === 'single' ? 'bg-emerald-600 border-2 border-yellow-300' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow transition">👤 Single Student Reset</button>
                            <button @click="resetScope = 'class'" :class="resetScope === 'class' ? 'bg-red-600 border-2 border-yellow-300' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow transition">🏫 Entire Class Bulk Reset</button>
                            <button @click="resetScope = 'date'" :class="resetScope === 'date' ? 'bg-red-700 border-2 border-yellow-300' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow transition">📅 Specific Date Reset</button>
                        </div>
                    </div>
                    <div class="grid grid-cols-3 gap-4">
                        <select x-show="resetScope === 'single'" x-model="resetReg" class="w-full p-3 rounded-xl">
                            <option value="">--- Select Student ---</option>
                            <template x-for="st in students"><option :value="st.reg_no" x-text="st.reg_no + ' - ' + st.name"></option></template>
                        </select>
                        <select x-model="resetSubject" class="w-full p-3 rounded-xl">
                            <option value="All Subjects">All Subjects</option>
                            <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                        </select>
                        <input type="date" x-show="resetScope === 'date'" x-model="resetDate" class="w-full p-3 rounded-xl">
                    </div>
                    <button @click="executeReset" class="bg-red-600 hover:bg-red-700 text-white font-black py-3 px-6 rounded-xl shadow-lg">⚠️ Execute Purge / Reset Records</button>
                </div>
            </div>

            <!-- TAB 6: MANAGE STUDENTS -->
            <div x-show="currentTab === 'students'" class="space-y-6">
                <h2 class="text-2xl font-black text-white mb-2">👥 Database Management</h2>
                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl border-2 border-blue-400">
                        <h3 class="text-xl font-black text-blue-400 mb-2">1️⃣ Register New Students (Excel/CSV)</h3>
                        <p class="text-xs text-slate-300 mb-4">Upload a file containing Roll No, Reg No, and Name.</p>
                        <input type="file" id="studentOnlyFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-blue-50 text-slate-900">
                        <button @click="importStudentsOnly" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-black py-3 rounded-xl shadow transition">Add Students to Database</button>

                        <div x-show="skippedImports.length > 0" class="mt-4 bg-red-900/40 border border-red-500/50 p-4 rounded-xl text-xs" style="display: none;">
                            <h4 class="text-red-400 font-bold mb-2">⚠️ Skipped (Already in another class):</h4>
                            <ul class="list-disc pl-4 text-slate-300 max-h-32 overflow-y-auto custom-scrollbar">
                                <template x-for="reg in skippedImports"><li x-text="reg"></li></template>
                            </ul>
                        </div>
                    </div>

                    <div class="glass-card p-6 rounded-2xl border-2 border-emerald-400">
                        <h3 class="text-xl font-black text-emerald-400 mb-2">2️⃣ Bulk Mark Attendance (Excel/CSV)</h3>
                        <p class="text-xs text-slate-300 mb-4">Select Date & Subject, then upload file with "P/A" status.</p>
                        <div class="grid grid-cols-2 gap-4 mb-4">
                            <div>
                                <label class="block text-white font-bold text-xs mb-1">Select Subject</label>
                                <select x-model="importSubject" class="w-full p-2.5 rounded-xl text-sm">
                                    <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                                </select>
                            </div>
                            <div>
                                <label class="block text-white font-bold text-xs mb-1">Select Date</label>
                                <input type="date" x-model="importDate" class="w-full p-2.5 rounded-xl text-sm">
                            </div>
                        </div>
                        <input type="file" id="attendanceFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-emerald-50 text-slate-900">
                        <button @click="importAttendanceOnly" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl shadow transition">Mark Attendance from File</button>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">➕ Add Single Student Manually</h3>
                        <form @submit.prevent="addStudent" class="space-y-4">
                            <input type="text" x-model="newStudent.reg_no" placeholder="Registration No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.roll_no" placeholder="Roll No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.name" placeholder="Full Name" required class="w-full p-3 rounded-xl">
                            <button type="submit" class="w-full bg-blue-500 hover:bg-blue-600 text-white font-black py-3 rounded-xl shadow">Save Student</button>
                        </form>
                    </div>

                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">📸 Upload Photo & Profile Details</h3>
                        <form @submit.prevent="saveStudentProfile" class="space-y-3">
                            <select x-model="profileReg" class="w-full p-2.5 rounded-xl">
                                <option value="">--- Select Student ---</option>
                                <template x-for="st in students">
                                    <option :value="st.reg_no" x-text="st.reg_no + ' - ' + st.name"></option>
                                </template>
                            </select>
                            <input type="file" id="studentPhotoFile" class="w-full p-2 rounded-xl text-sm bg-sky-50">
                            <input type="email" x-model="profileForm.email" placeholder="Email ID" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.contact" placeholder="Student Contact No" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.parent_name" placeholder="Parent / Guardian Name" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.parent_contact" placeholder="Parent Contact No" class="w-full p-2 rounded-xl text-sm">
                            <select x-model="profileForm.res_type" class="w-full p-2.5 rounded-xl text-sm">
                                <option>🏠 HOSTELER (Hostel Resident)</option>
                                <option>🚌 DAY SCHOLAR (Regular / Up-Down)</option>
                            </select>
                            <button type="submit" class="w-full bg-blue-500 hover:bg-blue-600 text-white font-black py-2.5 rounded-xl shadow">Save Complete Profile</button>
                        </form>
                    </div>

                    <!-- DANGER ZONE -->
                    <div class="glass-card p-6 rounded-2xl col-span-2 border-2 border-red-500/50">
                        <h3 class="text-xl font-black text-red-400 mb-4">⚠️ Danger Zone: Delete All Students</h3>
                        <p class="text-sm text-slate-300 mb-4">This action will permanently remove all students, their personal details, leaves, and attendance records from your database.</p>
                        <button @click="deleteAllStudents" class="w-full bg-red-700 hover:bg-red-800 text-white font-black py-3 rounded-xl shadow">Delete All Students & Data Forever</button>
                    </div>
                </div>
            </div>

            <!-- TAB 7: FACULTY LEAVE REQUESTS -->
            <div x-show="currentTab === 'leaves'" class="space-y-6">
                <div class="flex justify-between items-center">
                    <div>
                        <h2 class="text-2xl font-black text-white">📩 Student Leave Applications</h2>
                        <p class="text-slate-300 text-sm">Review, approve or reject leaves submitted by your class students with custom feedback.</p>
                    </div>
                    <button @click="loadFacultyLeaves()" class="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-xl font-bold text-xs shadow">🔄 Refresh List</button>
                </div>

                <div class="glass-card p-6 rounded-3xl">
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm">
                            <thead class="bg-blue-950 text-sky-400 font-bold border-b border-sky-500/30">
                                <tr>
                                    <th class="p-3">Applied On</th>
                                    <th class="p-3">Student Details</th>
                                    <th class="p-3">Type & Subject</th>
                                    <th class="p-3">Dates</th>
                                    <th class="p-3">Reason</th>
                                    <th class="p-3">Status</th>
                                    <th class="p-3 text-center">Actions & Message</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-slate-700/60">
                                <template x-for="leave in facultyLeaves" :key="leave.id">
                                    <tr class="hover:bg-slate-800/40">
                                        <td class="p-3 text-xs text-slate-400" x-text="leave.applied_on"></td>
                                        <td class="p-3">
                                            <p class="font-black text-white" x-text="leave.student_name"></p>
                                            <p class="text-xs text-sky-400 font-bold" x-text="'Reg: ' + leave.reg_no"></p>
                                        </td>
                                        <td class="p-3">
                                            <span class="bg-slate-800 text-amber-400 font-bold px-2 py-0.5 rounded text-xs" x-text="leave.leave_type"></span>
                                            <p class="text-xs text-slate-300 mt-1 font-semibold" x-text="'Subject: ' + leave.subject"></p>
                                        </td>
                                        <td class="p-3 text-xs font-bold text-slate-200">
                                            <p x-text="'From: ' + leave.from_date"></p>
                                            <p x-text="'To: ' + leave.to_date"></p>
                                        </td>
                                        <td class="p-3 max-w-xs">
                                            <p class="text-xs text-slate-300 italic mb-1" x-text="leave.reason"></p>
                                        </td>
                                        <td class="p-3">
                                            <span class="px-2.5 py-1 rounded-full text-xs font-black"
                                                  :class="leave.status === 'Approved' ? 'bg-emerald-900 text-emerald-300 border border-emerald-500' : (leave.status === 'Rejected' ? 'bg-red-900 text-red-300 border border-red-500' : 'bg-amber-900 text-amber-300 border border-amber-500')"
                                                  x-text="leave.status"></span>
                                            <p x-show="leave.faculty_remark" class="text-[11px] text-slate-400 mt-1 font-semibold" x-text="'Note: ' + leave.faculty_remark"></p>
                                        </td>
                                        <td class="p-3 text-center">
                                            <template x-if="leave.status === 'Pending'">
                                                <div class="flex flex-col gap-2 min-w-[200px]">
                                                    <input type="text" x-model="leaveRemarkInput[leave.id]" placeholder="Faculty Remark/Message..." class="w-full p-1.5 rounded-lg text-xs bg-white text-slate-900">
                                                    <div class="flex gap-2">
                                                        <button @click="handleLeaveAction(leave.id, 'Approved')" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-1.5 rounded-lg text-xs shadow">✅ Approve</button>
                                                        <button @click="handleLeaveAction(leave.id, 'Rejected')" class="flex-1 bg-red-600 hover:bg-red-700 text-white font-bold py-1.5 rounded-lg text-xs shadow">❌ Reject</button>
                                                    </div>
                                                </div>
                                            </template>
                                            <template x-if="leave.status !== 'Pending'">
                                                <span class="text-xs text-slate-400 italic">Action Completed</span>
                                            </template>
                                        </td>
                                    </tr>
                                </template>
                                <tr x-show="facultyLeaves.length === 0">
                                    <td colspan="7" class="text-center p-8 text-slate-400 font-bold">No leave applications found.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB 8: COLLEGE PROFILE -->
            <div x-show="currentTab === 'profile'">
                <h2 class="text-2xl font-black text-white mb-4">🏢 Core Settings</h2>
                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">🏫 Institutional Config</h3>
                        <form @submit.prevent="saveCollegeProfile" class="space-y-4">
                            <div>
                                <label class="block text-sky-400 font-bold text-xs mb-1">College Name</label>
                                <input type="text" x-model="collegeName" required class="w-full p-3 rounded-xl">
                            </div>
                            <div>
                                <label class="block text-sky-400 font-bold text-xs mb-1">Subtitle</label>
                                <input type="text" x-model="appSubtitle" required class="w-full p-3 rounded-xl">
                            </div>
                            <div>
                                <label class="block text-sky-400 font-bold text-xs mb-1">Course</label>
                                <input type="text" x-model="courseName" required class="w-full p-3 rounded-xl">
                            </div>
                            <div>
                                <label class="block text-sky-400 font-bold text-xs mb-1">Semester / Section</label>
                                <input type="text" x-model="sectionName" required class="w-full p-3 rounded-xl">
                            </div>
                            <button type="submit" class="w-full bg-blue-500 text-white font-black py-3 rounded-xl shadow">Save Metadata</button>
                        </form>
                    </div>
                    <div class="space-y-6">
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">📚 Add Subject</h3>
                            <form @submit.prevent="addSubject" class="space-y-4">
                                <input type="text" x-model="newSubject" placeholder="Subject Name" required class="w-full p-3 rounded-xl">
                                <button type="submit" class="w-full bg-blue-500 text-white font-black py-3 rounded-xl shadow">Add Subject</button>
                            </form>
                        </div>
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">🗑️ Delete Subject</h3>
                            <form @submit.prevent="deleteSubject" class="space-y-4">
                                <select x-model="delSubject" class="w-full p-3 rounded-xl">
                                    <option value="">--- Select Subject ---</option>
                                    <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                                </select>
                                <button type="submit" class="w-full bg-red-500 text-white font-black py-3 rounded-xl shadow">Delete Subject</button>
                            </form>
                        </div>
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">🖼️ College Logo (Cloud Secured)</h3>
                            <input type="file" id="logoFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-sky-50">
                            <button @click="uploadLogo" class="w-full bg-blue-500 hover:bg-blue-600 text-white font-black py-3 rounded-xl shadow">Upload Logo to Cloud</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- ============================================== -->
    <!-- STUDENT READ-ONLY & LEAVE DASHBOARD            -->
    <!-- ============================================== -->
    <div x-show="loggedIn && userRole === 'student'" class="min-h-screen relative z-10 p-4 md:p-8" style="display: none;">
        <!-- Header -->
        <div class="max-w-5xl mx-auto glass-card p-6 rounded-3xl shadow-2xl mb-8 flex justify-between items-center border-t-4 border-emerald-500">
            <div class="flex items-center gap-4">
                <img :src="studentDashData?.logo || 'https://i.ibb.co/3s68K1v/tree-logo.png'" class="w-16 h-16 bg-white rounded-xl p-1 shadow object-contain">
                <div>
                    <h1 class="text-2xl font-black text-white" x-text="studentDashData?.college_name"></h1>
                    <p class="text-emerald-400 font-bold text-sm" x-text="studentDashData?.course"></p>
                </div>
            </div>
            <div class="flex items-center gap-3">
                <button @click="openLeaveEmailModal()" class="bg-indigo-600 hover:bg-indigo-700 text-white font-black px-5 py-2.5 rounded-xl shadow-lg transition flex items-center gap-2">
                    ✉️ Apply Leave (Email Form)
                </button>
                <button @click="logout" class="bg-red-500 hover:bg-red-600 text-white font-black px-6 py-2.5 rounded-xl shadow-lg transition">🚪 Logout</button>
            </div>
        </div>

        <div class="max-w-5xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-8" x-show="studentDashData">
            <!-- Left Profile Card -->
            <div class="col-span-1 space-y-6">
                <div class="bg-slate-900 border-2 border-emerald-500/30 p-6 rounded-3xl shadow-2xl text-center">
                    <div class="text-6xl mb-4">🎓</div>
                    <h2 class="text-2xl font-black text-white" x-text="studentDashData.student.name"></h2>
                    <p class="text-sky-300 font-bold mt-1">Roll No: <span x-text="studentDashData.student.roll_no"></span></p>
                    <p class="text-slate-400 font-bold text-xs mt-1">Reg No: <span x-text="studentDashData.student.reg_no"></span></p>

                    <div class="mt-8 pt-6 border-t border-slate-700">
                        <p class="text-slate-400 font-bold text-sm mb-2">Overall Attendance</p>
                        <div class="flex justify-center items-center">
                            <div class="w-32 h-32 rounded-full border-8 flex items-center justify-center text-3xl font-black shadow-[0_0_15px_rgba(16,185,129,0.5)]"
                                 :class="studentDashData.overall_pct >= 75 ? 'border-emerald-500 text-emerald-400' : 'border-red-500 text-red-400'"
                                 x-text="studentDashData.overall_pct + '%'">
                            </div>
                        </div>
                        <p class="mt-4 text-xs font-bold" :class="studentDashData.overall_pct >= 75 ? 'text-emerald-400' : 'text-red-400'">
                            <span x-text="studentDashData.overall_pct >= 75 ? '✅ Safe Zone' : '⚠️ Shortage / Defaulter Zone'"></span>
                        </p>
                    </div>
                </div>
            </div>

            <!-- Right Details Area -->
            <div class="col-span-1 md:col-span-2 space-y-8">
                <!-- Subject Wise Compile Report -->
                <div class="glass-card p-6 rounded-3xl">
                    <h3 class="text-xl font-black text-amber-400 mb-6 flex items-center gap-2">📊 Subject-wise Compilation Report</h3>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <template x-for="sub in studentDashData.summary">
                            <div class="bg-slate-800/80 p-4 rounded-2xl border border-slate-700">
                                <div class="flex justify-between items-end mb-2">
                                    <h4 class="font-bold text-sky-300 truncate w-3/4" x-text="sub.subject"></h4>
                                    <span class="font-black text-lg" :class="sub.pct >= 75 ? 'text-emerald-400' : 'text-amber-400'" x-text="sub.pct + '%'"></span>
                                </div>
                                <div class="w-full bg-slate-900 rounded-full h-2.5 mb-2">
                                    <div class="h-2.5 rounded-full" :class="sub.pct >= 75 ? 'bg-emerald-500' : 'bg-amber-500'" :style="'width: ' + sub.pct + '%'"></div>
                                </div>
                                <p class="text-xs font-bold text-slate-400">Classes: <span class="text-white" x-text="sub.present + ' / ' + sub.total"></span></p>
                            </div>
                        </template>
                    </div>
                </div>

                <!-- Daily Attendance Register -->
                <div class="glass-card p-6 rounded-3xl">
                    <h3 class="text-xl font-black text-emerald-400 mb-4 flex items-center gap-2">📅 Daily Attendance Register (P/A History)</h3>
                    <div class="max-h-64 overflow-y-auto pr-2 custom-scrollbar">
                        <table class="w-full text-left text-sm">
                            <thead class="sticky top-0 bg-slate-900/90 text-sky-400 font-bold backdrop-blur">
                                <tr>
                                    <th class="p-3 rounded-tl-lg">Date</th>
                                    <th class="p-3">Subject</th>
                                    <th class="p-3 text-center rounded-tr-lg">Status</th>
                                </tr>
                            </thead>
                            <tbody class="text-slate-200 font-semibold">
                                <template x-for="rec in studentDashData.history">
                                    <tr class="border-b border-slate-700/50 hover:bg-slate-800/50">
                                        <td class="p-3" x-text="rec.date"></td>
                                        <td class="p-3" x-text="rec.subject"></td>
                                        <td class="p-3 text-center">
                                            <span class="px-3 py-1 rounded-full text-xs font-black shadow"
                                                  :class="rec.status === 'Present' ? 'bg-emerald-900 text-emerald-400 border border-emerald-500' : 'bg-red-900 text-red-400 border border-red-500'"
                                                  x-text="rec.status === 'Present' ? 'P' : 'A'"></span>
                                        </td>
                                    </tr>
                                </template>
                                <tr x-show="studentDashData.history.length === 0">
                                    <td colspan="3" class="text-center p-6 text-slate-500">No attendance records found yet.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- NEW: My Submitted Leaves -->
                <div class="glass-card p-6 rounded-3xl border-2 border-indigo-400/40">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-black text-indigo-300 flex items-center gap-2">📬 My Submitted Leave Applications</h3>
                        <button @click="openLeaveEmailModal()" class="text-xs bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-1.5 px-3 rounded-lg shadow">+ New Application</button>
                    </div>
                    <div class="max-h-64 overflow-y-auto pr-2 custom-scrollbar">
                        <table class="w-full text-left text-sm">
                            <thead class="sticky top-0 bg-slate-900/90 text-sky-400 font-bold backdrop-blur">
                                <tr>
                                    <th class="p-3">Applied On</th>
                                    <th class="p-3">Type / Subject</th>
                                    <th class="p-3">Date Range</th>
                                    <th class="p-3">Status</th>
                                    <th class="p-3">Faculty Remark</th>
                                </tr>
                            </thead>
                            <tbody class="text-slate-200">
                                <template x-for="l in studentDashData.leaves" :key="l.id">
                                    <tr class="border-b border-slate-700/50 hover:bg-slate-800/40">
                                        <td class="p-3 text-xs text-slate-400" x-text="l.applied_on"></td>
                                        <td class="p-3">
                                            <span class="bg-slate-800 text-amber-300 font-bold px-2 py-0.5 rounded text-xs" x-text="l.leave_type"></span>
                                            <p class="text-xs text-slate-400 mt-0.5" x-text="l.subject"></p>
                                        </td>
                                        <td class="p-3 text-xs font-bold" x-text="l.from_date + ' to ' + l.to_date"></td>
                                        <td class="p-3">
                                            <span class="px-2.5 py-0.5 rounded-full text-xs font-black"
                                                  :class="l.status === 'Approved' ? 'bg-emerald-900 text-emerald-300 border border-emerald-500' : (l.status === 'Rejected' ? 'bg-red-900 text-red-300 border border-red-500' : 'bg-amber-900 text-amber-300 border border-amber-500')"
                                                  x-text="l.status"></span>
                                        </td>
                                        <td class="p-3 text-xs text-slate-300 italic" x-text="l.faculty_remark || '---'"></td>
                                    </tr>
                                </template>
                                <tr x-show="studentDashData.leaves.length === 0">
                                    <td colspan="5" class="text-center p-6 text-slate-500">No leave applications submitted yet.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- ============================================== -->
    <!-- MODAL 1: STUDENT LEAVE APPLICATION (EMAIL GUI) -->
    <!-- ============================================== -->
    <div x-show="showLeaveModal" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 z-50" style="display: none;">
        <div class="bg-slate-900 border-2 border-indigo-400 p-6 md:p-8 rounded-3xl shadow-2xl w-full max-w-2xl text-slate-900 max-h-[90vh] overflow-y-auto custom-scrollbar relative">
            <div class="flex justify-between items-center border-b border-slate-700 pb-4 mb-4">
                <div class="flex items-center gap-2">
                    <span class="text-2xl">✉️</span>
                    <h3 class="text-xl font-black text-white">Compose Leave Application</h3>
                </div>
                <button @click="showLeaveModal = false" class="text-slate-400 hover:text-white text-2xl font-black">&times;</button>
            </div>

            <form @submit.prevent="submitLeaveApplication" class="space-y-4">
                <div class="bg-slate-950 p-3 rounded-xl border border-slate-800 text-xs text-slate-300 flex justify-between">
                    <span><b>From:</b> <span class="text-emerald-400" x-text="studentDashData?.student?.name + ' (' + studentDashData?.student?.reg_no + ')'"></span></span>
                    <span><b>To:</b> <span class="text-sky-400">Class Faculty Portal</span></span>
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Leave Category</label>
                        <select x-model="leaveForm.leave_type" required class="w-full p-2.5 rounded-xl text-sm">
                            <option>Sick / Medical Leave</option>
                            <option>Event / Sports Leave</option>
                            <option>Personal / Family Leave</option>
                            <option>Official College Duty</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Subject Scope</label>
                        <select x-model="leaveForm.subject" required class="w-full p-2.5 rounded-xl text-sm">
                            <option>All Subjects</option>
                            <template x-for="sub in studentDashData?.summary || []">
                                <option :value="sub.subject" x-text="sub.subject"></option>
                            </template>
                        </select>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">From Date</label>
                        <input type="date" x-model="leaveForm.from_date" required class="w-full p-2.5 rounded-xl text-sm">
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">To Date</label>
                        <input type="date" x-model="leaveForm.to_date" required class="w-full p-2.5 rounded-xl text-sm">
                    </div>
                </div>

                <div>
                    <label class="block text-sky-400 font-bold text-xs mb-1">Reason / Email Body</label>
                    <textarea x-model="leaveForm.reason" rows="4" placeholder="Dear Faculty, I am writing to formally request leave because..." required class="w-full p-3 rounded-xl text-sm font-semibold"></textarea>
                </div>

                <div class="flex gap-4 pt-4 border-t border-slate-700">
                    <button type="button" @click="showLeaveModal = false" class="flex-1 bg-slate-700 hover:bg-slate-600 text-white font-bold py-3 rounded-xl text-sm">Discard</button>
                    <button type="submit" class="flex-1 bg-indigo-600 hover:bg-indigo-700 text-white font-black py-3 rounded-xl text-sm shadow-xl">🚀 Send Application</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ============================================== -->
    <!-- MODAL 2: DEFAULTERS LIST POPUP (< 75%)         -->
    <!-- ============================================== -->
    <div x-show="showDefaultersModal" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 z-50" style="display: none;">
        <div class="bg-slate-900 border-2 border-red-500 p-6 md:p-8 rounded-3xl shadow-2xl w-full max-w-4xl text-white max-h-[90vh] overflow-y-auto custom-scrollbar relative">
            <div class="flex justify-between items-center border-b border-red-500/40 pb-4 mb-4">
                <div>
                    <h3 class="text-2xl font-black text-red-400 flex items-center gap-2">⚠️ Attendance Defaulters Roster</h3>
                    <p class="text-xs text-slate-300">Criteria: Attendance strictly below 75% for current month.</p>
                </div>
                <button @click="showDefaultersModal = false" class="text-slate-400 hover:text-white text-2xl font-black">&times;</button>
            </div>

            <div class="grid grid-cols-3 gap-4 mb-6">
                <div class="bg-slate-800 p-3 rounded-xl border border-slate-700">
                    <span class="text-xs text-slate-400 font-bold">Month / Year:</span>
                    <p class="text-sm font-black text-amber-400" x-text="selectedMonth + ' ' + selectedYear"></p>
                </div>
                <div class="bg-slate-800 p-3 rounded-xl border border-slate-700">
                    <span class="text-xs text-slate-400 font-bold">Total Classes Conducted:</span>
                    <p class="text-sm font-black text-purple-400" x-text="defaulterData?.total_classes || 0"></p>
                </div>
                <div class="bg-slate-800 p-3 rounded-xl border border-slate-700">
                    <span class="text-xs text-slate-400 font-bold">Defaulters Identified:</span>
                    <p class="text-sm font-black text-red-400" x-text="(defaulterData?.defaulters || []).length"></p>
                </div>
            </div>

            <div class="bg-sky-100 rounded-2xl overflow-hidden border-2 border-red-400 shadow-xl mb-6">
                <table class="w-full text-slate-900 font-bold text-sm text-center">
                    <thead class="bg-red-900 text-white">
                        <tr>
                            <th class="p-3 border">Roll No</th>
                            <th class="p-3 border">Reg No</th>
                            <th class="p-3 border text-left">Student Name</th>
                            <th class="p-3 border">Attended / Total</th>
                            <th class="p-3 border">Overall %</th>
                            <th class="p-3 border">Shortage %</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-red-200">
                        <template x-for="d in defaulterData?.defaulters || []">
                            <tr class="bg-red-50 hover:bg-red-100">
                                <td class="p-3 border" x-text="d.roll_no"></td>
                                <td class="p-3 border" x-text="d.reg_no"></td>
                                <td class="p-3 border text-left" x-text="d.name"></td>
                                <td class="p-3 border" x-text="d.attended + ' / ' + d.total_classes"></td>
                                <td class="p-3 border font-black text-red-700" x-text="d.overall_pct + '%'"></td>
                                <td class="p-3 border text-red-600" x-text="'-' + d.shortage + '%'"></td>
                            </tr>
                        </template>
                        <tr x-show="(defaulterData?.defaulters || []).length === 0">
                            <td colspan="6" class="text-center p-6 text-emerald-700 font-black">🎉 No defaulters! All registered students have >= 75% attendance.</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="flex justify-end gap-4">
                <button @click="showDefaultersModal = false" class="bg-slate-700 hover:bg-slate-600 px-6 py-2.5 rounded-xl font-bold text-sm">Close</button>
            </div>
        </div>
    </div>

    <!-- ============================================== -->
    <!-- JAVASCRIPT / ALPINE LOGIC                      -->
    <!-- ============================================== -->
    <script>
        function erpApp() {
            let now = new Date();
            let mList = ['January','February','March','April','May','June','July','August','September','October','November','December'];
            let yList = ['2025', '2026', '2027'];

            let curMonth = mList[now.getMonth()];
            let curYear = String(now.getFullYear());
            let curDate = now.toISOString().split('T')[0];

            return {
                // CORE AUTH
                loggedIn: false,
                userRole: '',
                authRole: 'faculty',
                isLogin: true,
                authForm: { username: '', password: '' },
                studentForm: { reg_no: '', name: '' },
                authError: '',
                userId: '',
                currentFacultyIdForStudent: '',
                currentTab: 'dashboard',

                // CONFIG & STATS
                months: mList,
                years: yList,
                collegeName: 'INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)',
                appSubtitle: 'ATTENDANCE MANAGEMENT SYSTEM',
                courseName: 'BCA',
                sectionName: 'Semester 1',
                collegeLogo: 'https://i.ibb.co/3s68K1v/tree-logo.png',

                totalStudents: 0,
                classesConducted: 0,
                presentToday: 0,
                subjects: [],
                selectedSubject: '',
                selectedMonth: curMonth,
                selectedYear: curYear,
                selectedDate: curDate,

                students: [],
                currentIndex: 0,
                currentStudentDetails: {},
                currentStudentPhoto: '',
                searchReg: '',
                newStudent: { reg_no: '', roll_no: '', name: '' },
                delRegNo: '',
                newSubject: '',
                delSubject: '',
                profileReg: '',
                profileForm: { email: '', contact: '', parent_name: '', parent_contact: '', res_type: '🏠 HOSTELER (Hostel Resident)' },

                tableMonth: curMonth,
                tableYear: curYear,
                tableSubject: '',
                tableNumDays: 31,
                tableRows: [],
                tableTotalClasses: 0,

                reportMonth: curMonth,
                reportYear: curYear,
                reportSubjects: [],
                reportRows: [],

                resetScope: 'single',
                resetReg: '',
                resetSubject: 'All Subjects',
                resetDate: curDate,

                importSubject: '',
                importDate: curDate,

                studentDashData: null,
                tableSearchQuery: '',
                reportSearchQuery: '',
                skippedImports: [],

                // LEAVE SYSTEM STATES
                showLeaveModal: false,
                leaveForm: {
                    leave_type: 'Sick / Medical Leave',
                    subject: 'All Subjects',
                    from_date: curDate,
                    to_date: curDate,
                    reason: ''
                },
                facultyLeaves: [],
                leaveRemarkInput: {},

                // DEFAULTERS STATES
                showDefaultersModal: false,
                defaulterData: null,

                get pendingLeavesCount() {
                    return this.facultyLeaves.filter(l => l.status === 'Pending').length;
                },

                init() {
                    this.syncFromDate();
                },

                syncFromDate() {
                    if (!this.selectedDate) return;
                    let parts = this.selectedDate.split('-');
                    if (parts.length === 3) {
                        let y = parts[0];
                        let mIdx = parseInt(parts[1], 10) - 1;
                        if (mIdx >= 0 && mIdx < 12) {
                            this.selectedMonth = this.months[mIdx];
                            this.selectedYear = y;
                            this.tableMonth = this.selectedMonth;
                            this.tableYear = this.selectedYear;
                            this.reportMonth = this.selectedMonth;
                            this.reportYear = this.selectedYear;
                        }
                    }
                },

                syncToLive() {
                    this.tableMonth = this.selectedMonth;
                    this.tableYear = this.selectedYear;
                    this.reportMonth = this.selectedMonth;
                    this.reportYear = this.selectedYear;
                },

                async submitAuth() {
                    let endpoint = this.isLogin ? '/api/login' : '/api/register';
                    let formData = new FormData();
                    formData.append('username', this.authForm.username);
                    formData.append('password', this.authForm.password);
                    try {
                        let res = await fetch(endpoint, { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            this.userRole = 'faculty';
                            this.userId = this.authForm.username;
                            this.loggedIn = true;
                            this.authError = '';
                            this.loadData();
                            this.loadFacultyLeaves();
                        } else {
                            this.authError = data.detail || "Authentication Failed. Please try again.";
                        }
                    } catch(e) {
                        this.authError = "Server Connection Error. Check Backend.";
                    }
                },

                async submitStudentAuth() {
                    let formData = new FormData();
                    formData.append('reg_no', this.studentForm.reg_no);
                    formData.append('name', this.studentForm.name);
                    try {
                        let res = await fetch('/api/student_login', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            this.userRole = 'student';
                            this.userId = data.reg_no; 
                            this.currentFacultyIdForStudent = data.faculty_id;
                            this.loggedIn = true;
                            this.authError = '';
                            this.loadStudentDashboard(data.faculty_id, data.reg_no);
                        } else {
                            this.authError = data.detail || "Student Login Failed.";
                        }
                    } catch(e) {
                        this.authError = "Server Connection Error.";
                    }
                },

                async loadStudentDashboard(fac_id, reg_no) {
                    try {
                        let res = await fetch(`/api/student_dashboard_data/${encodeURIComponent(fac_id)}/${encodeURIComponent(reg_no)}`);
                        if (!res.ok) {
                            let err = await res.json();
                            alert(err.detail || "Error loading dashboard data.");
                            this.logout();
                            return;
                        }
                        let data = await res.json();
                        if (data.error) {
                            alert(data.error);
                            this.logout();
                        } else {
                            this.studentDashData = data;
                        }
                    } catch (e) {
                        alert("Error loading dashboard data: " + e.message);
                    }
                },

                openLeaveEmailModal() {
                    this.leaveForm = {
                        leave_type: 'Sick / Medical Leave',
                        subject: 'All Subjects',
                        from_date: this.selectedDate,
                        to_date: this.selectedDate,
                        reason: ''
                    };
                    this.showLeaveModal = true;
                },

                async submitLeaveApplication() {
                    if (!this.leaveForm.reason.trim()) {
                        alert("Please enter a reason for the leave application.");
                        return;
                    }

                    let formData = new FormData();
                    formData.append('faculty_id', this.currentFacultyIdForStudent);
                    formData.append('reg_no', this.studentDashData.student.reg_no);
                    formData.append('student_name', this.studentDashData.student.name);
                    formData.append('leave_type', this.leaveForm.leave_type);
                    formData.append('subject', this.leaveForm.subject);
                    formData.append('from_date', this.leaveForm.from_date);
                    formData.append('to_date', this.leaveForm.to_date);
                    formData.append('reason', this.leaveForm.reason);

                    try {
                        let res = await fetch('/api/apply_leave', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.showLeaveModal = false;
                            this.loadStudentDashboard(this.currentFacultyIdForStudent, this.studentDashData.student.reg_no);
                        } else {
                            alert("Failed to submit: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error while submitting leave.");
                    }
                },

                async loadFacultyLeaves() {
                    try {
                        let res = await fetch(`/api/leaves/${encodeURIComponent(this.userId)}`);
                        let data = await res.json();
                        this.facultyLeaves = data.leaves || [];
                    } catch(e) {
                        console.error("Error loading faculty leaves: ", e);
                    }
                },

                async handleLeaveAction(leaveId, status) {
                    let remark = this.leaveRemarkInput[leaveId] || "";
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('leave_id', leaveId);
                    formData.append('status', status);
                    formData.append('remark', remark);

                    try {
                        let res = await fetch('/api/update_leave_status', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.loadFacultyLeaves();
                        } else {
                            alert("Error updating leave: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error while updating leave status.");
                    }
                },

                async openDefaultersModal() {
                    try {
                        let res = await fetch(`/api/defaulters/${encodeURIComponent(this.userId)}?month=${this.selectedMonth}&year=${this.selectedYear}&threshold=75.0`);
                        let data = await res.json();
                        this.defaulterData = data;
                        this.showDefaultersModal = true;
                    } catch(e) {
                        alert("Error loading defaulters list.");
                    }
                },

                async loadData() {
                    try {
                        let res = await fetch(`/api/data/${encodeURIComponent(this.userId)}?month=${this.selectedMonth}&year=${this.selectedYear}&subject=${encodeURIComponent(this.selectedSubject)}&target_date=${this.selectedDate}`);
                        let data = await res.json();
                        this.collegeName = data.college_name;
                        this.appSubtitle = data.app_subtitle;
                        this.courseName = data.course_name;
                        this.sectionName = data.section_name;
                        this.collegeLogo = data.college_logo || this.collegeLogo;
                        this.totalStudents = data.total_students;
                        this.classesConducted = data.classes_conducted;
                        this.presentToday = data.present_today;
                        this.subjects = data.subjects;
                        if (!this.selectedSubject && this.subjects.length > 0) this.selectedSubject = this.subjects[0];
                        if (!this.tableSubject && this.subjects.length > 0) this.tableSubject = this.subjects[0];
                        if (!this.importSubject && this.subjects.length > 0) this.importSubject = this.subjects[0];
                        this.students = data.students;
                        if (this.students.length > 0) this.fetchStudentDetails();
                    } catch(e) {
                        console.error("Dashboard Load Error: ", e);
                    }
                },

                async loadTableData() {
                    if (!this.tableSubject && this.subjects.length > 0) this.tableSubject = this.subjects[0];
                    let res = await fetch(`/api/attendance_table/${encodeURIComponent(this.userId)}?month=${this.tableMonth}&year=${this.tableYear}&subject=${encodeURIComponent(this.tableSubject)}`);
                    let data = await res.json();
                    this.tableNumDays = data.num_days;
                    this.tableRows = data.table_data;
                    this.tableTotalClasses = data.total_classes;
                },

                async toggleCellAttendance(student, day) {
                    let current = student.days[day];
                    let nextStatus = '';
                    let displayVal = '';

                    if (current === 'P') {
                        nextStatus = 'Absent';
                        displayVal = 'A';
                    } else if (current === 'A') {
                        nextStatus = 'Clear';
                        displayVal = '';
                    } else {
                        nextStatus = 'Present';
                        displayVal = 'P';
                    }

                    student.days[day] = displayVal;

                    let distinctDays = new Set();
                    for (let s of this.tableRows) {
                        for (let d = 1; d <= this.tableNumDays; d++) {
                            if (s.days[d] === 'P' || s.days[d] === 'A') {
                                distinctDays.add(d);
                            }
                        }
                    }
                    this.tableTotalClasses = distinctDays.size;

                    for (let s of this.tableRows) {
                        let pCount = 0;
                        for (let d = 1; d <= this.tableNumDays; d++) {
                            if (s.days[d] === 'P') pCount++;
                        }
                        s.pct = this.tableTotalClasses > 0 ? Math.round((pCount / this.tableTotalClasses) * 100) : 0;
                    }

                    let mIdx = this.months.indexOf(this.tableMonth) + 1;
                    let dateStr = `${this.tableYear}-${String(mIdx).padStart(2, '0')}-${String(day).padStart(2, '0')}`;

                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('student_id', student.id);
                    formData.append('subject', this.tableSubject);
                    formData.append('date_str', dateStr);
                    formData.append('status', nextStatus);

                    try {
                        let res = await fetch('/api/mark_attendance', { method: 'POST', body: formData });
                        if (!res.ok) {
                            let err = await res.json();
                            alert("Error updating attendance: " + err.detail);
                            this.loadTableData(); 
                        }
                    } catch(e) {
                        alert("Network error while updating attendance.");
                        this.loadTableData();
                    }
                },

                async loadReportData() {
                    let res = await fetch(`/api/compile_report/${encodeURIComponent(this.userId)}?month=${this.reportMonth}&year=${this.reportYear}`);
                    let data = await res.json();
                    this.reportSubjects = data.subjects;
                    this.reportRows = data.report;
                },

                get currentStudent() {
                    return this.students[this.currentIndex] || { name: '', reg_no: '', roll_no: '' };
                },

                get filteredTableRows() {
                    if (this.tableSearchQuery.trim() === '') return this.tableRows;
                    let q = this.tableSearchQuery.toLowerCase();
                    return this.tableRows.filter(st => 
                        (st.name && st.name.toLowerCase().includes(q)) || 
                        (st.reg_no && st.reg_no.toLowerCase().includes(q)) ||
                        (st.roll_no && String(st.roll_no).toLowerCase().includes(q))
                    );
                },

                get filteredReportRows() {
                    if (this.reportSearchQuery.trim() === '') return this.reportRows;
                    let q = this.reportSearchQuery.toLowerCase();
                    return this.reportRows.filter(st => 
                        (st.name && st.name.toLowerCase().includes(q)) || 
                        (st.reg_no && st.reg_no.toLowerCase().includes(q)) ||
                        (st.roll_no && String(st.roll_no).toLowerCase().includes(q))
                    );
                },

                async fetchStudentDetails() {
                    let reg = this.currentStudent.reg_no;
                    if (!reg) return;
                    let res = await fetch(`/api/student_details/${encodeURIComponent(this.userId)}/${encodeURIComponent(reg)}`);
                    let data = await res.json();
                    this.currentStudentDetails = data;
                    this.currentStudentPhoto = data.photo_data;
                },

                async markStatusBtn(status) {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('student_id', this.currentStudent.id);
                    formData.append('subject', this.selectedSubject);
                    formData.append('date_str', this.selectedDate);
                    formData.append('status', status);

                    let res = await fetch('/api/mark_attendance', { method: 'POST', body: formData });
                    if (res.ok) {
                        if (this.currentIndex < this.students.length - 1) {
                            this.currentIndex++;
                            this.fetchStudentDetails();
                        }
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error saving attendance: " + err.detail);
                    }
                },

                searchByReg() {
                    let idx = this.students.findIndex(s => s.reg_no.toLowerCase().includes(this.searchReg.toLowerCase()));
                    if (idx !== -1) {
                        this.currentIndex = idx;
                        this.fetchStudentDetails();
                    }
                },

                nextStudent() {
                    if (this.currentIndex < this.students.length - 1) {
                        this.currentIndex++;
                        this.fetchStudentDetails();
                    }
                },

                prevStudent() {
                    if (this.currentIndex > 0) {
                        this.currentIndex--;
                        this.fetchStudentDetails();
                    }
                },

                async addStudent() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.newStudent.reg_no);
                    formData.append('roll_no', this.newStudent.roll_no);
                    formData.append('name', this.newStudent.name);
                    let res = await fetch('/api/add_student', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.newStudent = { reg_no: '', roll_no: '', name: '' };
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error adding student: " + err.detail);
                    }
                },

                async deleteAllStudents() {
                    if (!confirm("WARNING: Are you entirely sure you want to delete ALL students, leaves, and their attendance data?")) return;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    let res = await fetch('/api/delete_all_students', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.loadData();
                        this.loadFacultyLeaves();
                    } else {
                        let err = await res.json();
                        alert("Error deleting records: " + err.detail);
                    }
                },

                async importStudentsOnly() {
                    this.skippedImports = [];
                    let fileInput = document.getElementById('studentOnlyFile');
                    if (fileInput.files.length === 0) { alert('Please select a CSV or Excel file.'); return; }

                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);

                    let res = await fetch('/api/import_students', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        if (data.skipped && data.skipped.length > 0) {
                            this.skippedImports = data.skipped;
                        }
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert('Import failed: ' + err.detail);
                    }
                },

                async importAttendanceOnly() {
                    let fileInput = document.getElementById('attendanceFile');
                    if (fileInput.files.length === 0) { alert('Please select a CSV or Excel file.'); return; }
                    if (!this.importSubject) { alert('Please select a subject.'); return; }
                    if (!this.importDate) { alert('Please select a date.'); return; }

                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    formData.append('subject', this.importSubject);
                    formData.append('date_str', this.importDate);

                    let res = await fetch('/api/import_attendance', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert('Import failed: ' + err.detail);
                    }
                },

                async executeReset() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('scope', this.resetScope);
                    formData.append('subject', this.resetSubject);
                    if (this.resetScope === 'single') formData.append('reg_no', this.resetReg);
                    if (this.resetScope === 'date') formData.append('date_str', this.resetDate);

                    let res = await fetch('/api/reset_attendance', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error resetting data: " + err.detail);
                    }
                },

                async addSubject() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.newSubject);
                    let res = await fetch('/api/add_subject', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.newSubject = '';
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error adding subject: " + err.detail);
                    }
                },

                async deleteSubject() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.delSubject);
                    let res = await fetch('/api/delete_subject', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.delSubject = '';
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error deleting subject: " + err.detail);
                    }
                },

                async uploadLogo() {
                    let fileInput = document.getElementById('logoFile');
                    if (fileInput.files.length === 0) { alert('Please select a logo file.'); return; }
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    let res = await fetch('/api/upload_logo', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        this.collegeLogo = data.logo_url;
                        alert(data.message);
                    } else {
                        let err = await res.json();
                        alert("Error uploading logo: " + err.detail);
                    }
                },

                async saveCollegeProfile() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('college_name', this.collegeName);
                    formData.append('subtitle', this.appSubtitle);
                    formData.append('course_name', this.courseName);
                    formData.append('section_name', this.sectionName);
                    let res = await fetch('/api/save_college_profile', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.loadData();
                    } else {
                        let err = await res.json();
                        alert("Error saving profile: " + err.detail);
                    }
                },

                async saveStudentProfile() {
                    if (!this.profileReg) { alert('Please select a student first.'); return; }
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.profileReg);
                    formData.append('email', this.profileForm.email);
                    formData.append('contact', this.profileForm.contact);
                    formData.append('parent_name', this.profileForm.parent_name);
                    formData.append('parent_contact', this.profileForm.parent_contact);
                    formData.append('res_type', this.profileForm.res_type);

                    let photoInput = document.getElementById('studentPhotoFile');
                    if (photoInput.files.length > 0) {
                        formData.append('file', photoInput.files[0]);
                    }

                    let res = await fetch('/api/save_student_profile', { method: 'POST', body: formData });
                    if (res.ok) {
                        let data = await res.json();
                        alert(data.message);
                        this.fetchStudentDetails();
                    } else {
                        let err = await res.json();
                        alert("Error saving student profile: " + err.detail);
                    }
                },

                async shareViaEmail() {
                    let pdfUrl = `/api/download_pdf/${encodeURIComponent(this.userId)}?month=${this.reportMonth}&year=${this.reportYear}`;
                    let fileName = `Attendance_Report_${this.reportMonth}_${this.reportYear}.pdf`;
                    try {
                        let response = await fetch(pdfUrl);
                        let blob = await response.blob();
                        let file = new File([blob], fileName, {type: "application/pdf"});
                        if (navigator.canShare && navigator.canShare({ files: [file] })) {
                            await navigator.share({ files: [file] });
                            return; 
                        } else {
                            throw new Error("Sharing not supported");
                        }
                    } catch(e) {
                        let a = document.createElement('a');
                        a.href = pdfUrl;
                        a.download = fileName;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        alert("PDF Downloaded successfully! You can now manually share the file.");
                    }
                },

                logout() {
                    this.loggedIn = false;
                    this.userRole = '';
                    this.userId = '';
                    this.currentFacultyIdForStudent = '';
                    this.studentDashData = null;
                    this.skippedImports = []; 
                    this.facultyLeaves = [];
                }
            }
        }
    </script>
</body>
</html>
"""