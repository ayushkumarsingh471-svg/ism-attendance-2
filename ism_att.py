import os
import io
import base64
import calendar
from datetime import datetime, date
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import create_engine, text

app = FastAPI(title="ISM Attendance ERP - Final Full Edition")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    try:
        import streamlit as st
        DATABASE_URL = st.secrets["DATABASE_URL"]
    except:
        DATABASE_URL = "postgresql://neondb_owner:npg_UD5M9QgOwLIi@ep-crimson-dew-axdarn17-pooler.c-4.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

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
    clean = "".join(c for c in uid if c.isalnum() or c == '_').lower()
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
        try:
            conn.execute(text(f'ALTER TABLE {t_details} ADD COLUMN IF NOT EXISTS photo_data TEXT'))
        except:
            pass
        
        res = conn.execute(text(f"SELECT COUNT(*) FROM {t_subjects}")).fetchone()[0]
        if res == 0:
            for sub in ['SAD', 'PST&PC', 'NT', 'BE', 'OS&UNIX LAB', 'PROG IN C LAB']:
                conn.execute(text(f"INSERT INTO {t_subjects} (subject_name) VALUES (:sub) ON CONFLICT DO NOTHING"), {"sub": sub})

# --- API ENDPOINTS ---

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    u = username.strip()
    with engine.begin() as conn:
        res = conn.execute(text("SELECT * FROM master_users WHERE username=:u AND password=:p"), {"u": u, "p": password}).fetchone()
    if res:
        init_tenant_db(u)
        return {"success": True, "user": u}
    raise HTTPException(status_code=400, detail="Invalid User ID or Password")

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
        raise HTTPException(status_code=400, detail="User ID already exists.")

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
            result.append({"reg_no": reg, "roll_no": roll, "name": name, "days": days_data, "pct": pct})

    return {"num_days": num_days, "table_data": result, "total_classes": tc_count}

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

        sub_total_classes = {}
        for sub, sub_id in sub_map.items():
            cnt = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            sub_total_classes[sub] = cnt

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
                tot_c = sub_total_classes.get(sub, 0)
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

        sub_total_classes = {sub: conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sid, "d": date_pattern}).fetchone()[0] or 0 for sub, sid in sub_map.items()}
        att_rows = conn.execute(text(f"SELECT student_id, subject_id, COUNT(*) FROM {t_attendance} WHERE status='Present' AND date LIKE :d GROUP BY student_id, subject_id"), {"d": date_pattern}).fetchall()
        present_map = {(r[0], r[1]): r[2] for r in att_rows}

        data = []
        for st in students:
            st_id, reg, roll, name = st
            row = {"Reg No": reg, "Roll No": roll, "Student Name": name}
            tot_p_all, tot_c_all = 0, 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub, 0)
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

        sub_total_classes = {sub: conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sid, "d": date_pattern}).fetchone()[0] or 0 for sub, sid in sub_map.items()}
        att_rows = conn.execute(text(f"SELECT student_id, subject_id, COUNT(*) FROM {t_attendance} WHERE status='Present' AND date LIKE :d GROUP BY student_id, subject_id"), {"d": date_pattern}).fetchall()
        present_map = {(r[0], r[1]): r[2] for r in att_rows}

        c_name = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key='college_name'")).fetchone()
        college_name = c_name[0] if c_name else "INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)"

        headers = ["Reg No", "Roll", "Name"] + subjects + ["Overall %"]
        table_data = [headers]

        for st in students:
            st_id, reg, roll, name = st
            row = [str(reg), str(roll), str(name)]
            tot_p_all, tot_c_all = 0, 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub, 0)
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
    safe_uid = get_safe_prefix(user_id)
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    with engine.begin() as conn:
        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        if not sub_id_res: raise HTTPException(status_code=400, detail="Subject not found")
        sub_id = sub_id_res[0]

        conn.execute(text(f"""
            INSERT INTO {t_attendance} (student_id, subject_id, date, status) 
            VALUES (:sid, :subid, :dt, :stat)
            ON CONFLICT (student_id, subject_id, date) 
            DO UPDATE SET status = :stat
        """), {"sid": student_id, "subid": sub_id, "dt": date_str, "stat": status})

    return {"success": True}

@app.post("/api/reset_attendance")
def reset_attendance(user_id: str = Form(...), scope: str = Form(...), reg_no: str = Form(None), subject: str = Form("All Subjects"), date_str: str = Form(None)):
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
    return {"success": True, "message": "Reset executed successfully!"}

@app.get("/api/student_details/{user_id}/{reg_no}")
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
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {t_students} (reg_no, roll_no, name) VALUES (:r, :ro, :n) ON CONFLICT (reg_no) DO NOTHING"), {"r": reg_no.strip(), "ro": roll_no.strip(), "n": name.strip()})
    return {"success": True}

@app.post("/api/delete_student")
def delete_student(user_id: str = Form(...), reg_no: str = Form(...)):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {t_students} WHERE reg_no=:r"), {"r": reg_no.strip()})
    return {"success": True}

@app.post("/api/bulk_import")
async def bulk_import(user_id: str = Form(...), file: UploadFile = File(...)):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    contents = await file.read()
    try:
        if file.filename.endswith('.csv'): df_raw = pd.read_csv(io.BytesIO(contents))
        else: df_raw = pd.read_excel(io.BytesIO(contents))
        if len(df_raw.columns) >= 3:
            df_clean = df_raw.iloc[:, :3].copy()
            df_clean.columns = ['reg_no', 'roll_no', 'name']
            df_clean = df_clean.dropna(subset=['reg_no', 'name'])
            with engine.begin() as conn:
                for _, row in df_clean.iterrows():
                    reg = str(row['reg_no']).strip()
                    if reg.endswith('.0'): reg = reg[:-2]
                    roll = str(row['roll_no']).strip()
                    if roll.endswith('.0'): roll = roll[:-2]
                    name = str(row['name']).strip()
                    if reg and name and reg.lower() != 'nan':
                        conn.execute(text(f"INSERT INTO {t_students} (reg_no, roll_no, name) VALUES (:r, :ro, :n) ON CONFLICT (reg_no) DO NOTHING"), {"r": reg, "ro": roll, "n": name})
            return {"success": True, "message": "Bulk import completed successfully!"}
        else: raise HTTPException(status_code=400, detail="File must have at least 3 columns")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/add_subject")
def add_subject(user_id: str = Form(...), subject_name: str = Form(...)):
    safe_uid = get_safe_prefix(user_id)
    t_subjects = f"{safe_uid}_subjects"
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {t_subjects} (subject_name) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": subject_name.strip()})
    return {"success": True}

@app.post("/api/delete_subject")
def delete_subject(user_id: str = Form(...), subject_name: str = Form(...)):
    safe_uid = get_safe_prefix(user_id)
    t_subjects = f"{safe_uid}_subjects"
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {t_subjects} WHERE subject_name=:s"), {"s": subject_name.strip()})
    return {"success": True}

@app.post("/api/upload_logo")
async def upload_logo(user_id: str = Form(...), file: UploadFile = File(...)):
    safe_uid = get_safe_prefix(user_id)
    t_settings = f"{safe_uid}_settings"
    contents = await file.read()
    ext = file.filename.split('.')[-1].lower()
    b64_val = f"data:image/{ext};base64,{base64.b64encode(contents).decode('utf-8')}"
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('college_logo', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": b64_val})
    return {"success": True, "logo_url": b64_val}

@app.post("/api/save_college_profile")
def save_college_profile(user_id: str = Form(...), college_name: str = Form(...), subtitle: str = Form(...), course_name: str = Form(...), section_name: str = Form(...)):
    safe_uid = get_safe_prefix(user_id)
    t_settings = f"{safe_uid}_settings"
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('college_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": college_name})
        conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('app_subtitle', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": subtitle})
        conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('course_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": course_name})
        conn.execute(text(f"INSERT INTO {t_settings} (key, value) VALUES ('section_name', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": section_name})
    return {"success": True}

@app.post("/api/save_student_profile")
async def save_student_profile(user_id: str = Form(...), reg_no: str = Form(...), email: str = Form(...), contact: str = Form(...), parent_name: str = Form(...), parent_contact: str = Form(...), res_type: str = Form(...), file: UploadFile = File(None)):
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
    return {"success": True}

# --- FULL HTML FRONTEND ---
@app.get("/", response_class=HTMLResponse)
def home():
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ISM Attendance ERP - Final Full Edition</title>
    <script src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" defer></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: radial-gradient(circle at 50% 30%, #1e3a8a 0%, #0f172a 60%, #020617 100%); min-height: 100vh; color: white; overflow-x: hidden; position: relative; }
        .anim-container { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; pointer-events: none; overflow: hidden; z-index: 0; }
        .floating-icon { position: absolute; bottom: -150px; opacity: 0.45; animation: floatUp 15s infinite linear; filter: drop-shadow(0 0 15px rgba(56, 189, 248, 0.9)); }
        @keyframes floatUp { 0% { transform: translateY(0) rotate(0deg) scale(0.9); opacity: 0; } 20% { opacity: 0.5; } 80% { opacity: 0.5; } 100% { transform: translateY(-115vh) rotate(360deg) scale(1.2); opacity: 0; } }
        .glass-card { background: rgba(15, 23, 42, 0.75) !important; backdrop-filter: blur(8px); border: 2px solid rgba(56, 189, 248, 0.3); }
        input, select, textarea { background-color: #e0f2fe !important; color: #0f172a !important; border: 2px solid #38bdf8 !important; font-weight: 800 !important; }
        input::placeholder { color: #64748b !important; }
        .math-grid-table th, .math-grid-table td { border: 2px solid #38bdf8 !important; }
        .math-grid-table th:nth-child(3), .math-grid-table td:nth-child(3) { min-width: 320px !important; text-align: left !important; padding-left: 14px !important; }
        .math-grid-table td:not(:nth-child(3)):not(:nth-child(1)):not(:nth-child(2)) { width: 34px !important; height: 34px !important; min-width: 34px !important; max-width: 34px !important; padding: 2px !important; font-size: 11px !important; }
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

    <!-- LOGIN SCREEN -->
    <div x-show="!loggedIn" class="flex items-center justify-center min-h-screen p-6 relative z-10">
        <div class="glass-card p-10 rounded-3xl shadow-2xl w-full max-w-4xl grid grid-cols-2 gap-8 items-center">
            <div>
                <div class="inline-block bg-sky-950/80 border border-sky-400/40 px-3 py-1 rounded-full text-xs font-bold text-sky-400 mb-4 shadow">⚡ ENTERPRISE CLOUD PORTAL</div>
                <h1 class="text-4xl font-black text-white mb-2">🎓 ISM PATNA</h1>
                <h3 class="text-lg font-bold text-amber-400 mb-4">ATTENDANCE ERP SYSTEM</h3>
                <p class="text-slate-300 text-xs leading-relaxed mb-6">Welcome to the professional Multi-Tenant Attendance ERP Platform. This portal provides complete data isolation, analytical insights, automated reports, and secure image profile mapping for individual courses and classes.</p>
                <div class="bg-sky-950/60 border border-sky-500/40 p-4 rounded-xl">
                    <p class="text-sky-300 font-bold text-xs mb-1">💡 Multi-Tenant Isolation Feature:</p>
                    <p class="text-slate-300 text-[11px]">Every class, course coordinator, or administrator can register a custom User ID to instantiate a clean, completely independent cloud database schema.</p>
                </div>
            </div>
            
            <div class="bg-slate-900/90 p-8 rounded-2xl border border-sky-400/30 shadow-2xl">
                <div class="flex items-center gap-2 mb-6">
                    <span class="text-2xl">🔐</span>
                    <h2 class="text-xl font-black text-white">Access Portal</h2>
                </div>
                <div class="flex gap-2 mb-6 bg-slate-950 p-1 rounded-xl">
                    <button @click="isLogin = true" :class="isLogin ? 'bg-cyan-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-2 font-bold rounded-lg transition text-xs">🔐 LOGIN</button>
                    <button @click="isLogin = false" :class="!isLogin ? 'bg-cyan-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-2 font-bold rounded-lg transition text-xs">📄 REGISTER NEW CLASS</button>
                </div>
                <form @submit.prevent="submitAuth" class="space-y-4">
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">User ID</label>
                        <input type="text" x-model="authForm.username" placeholder="Enter User ID" required class="w-full p-3 rounded-xl text-sm">
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Password</label>
                        <input type="password" x-model="authForm.password" placeholder="Enter Password" required class="w-full p-3 rounded-xl text-sm">
                    </div>
                    <button type="submit" class="w-full bg-cyan-500 hover:bg-cyan-600 text-white font-black py-3 rounded-xl shadow-lg transition text-sm">SECURE LOGIN</button>
                    <p x-text="authError" class="text-red-400 text-center text-xs font-bold mt-2"></p>
                </form>
            </div>
        </div>
    </div>

    <!-- MAIN APP INTERFACE -->
    <div x-show="loggedIn" class="flex h-screen overflow-hidden relative z-10" style="display: none;">
        <div class="w-72 bg-gradient-to-b from-blue-950 via-slate-950 to-slate-950 border-r-2 border-sky-400/50 flex flex-col justify-between p-4 shadow-2xl relative overflow-hidden">
            <div class="anim-container">
                <div class="floating-icon" style="left: 10%; animation-delay: 0s; font-size: 30px;">🎓</div>
                <div class="floating-icon" style="left: 70%; animation-delay: 5s; font-size: 25px;">🏆</div>
                <div class="floating-icon" style="left: 40%; animation-delay: 2s; font-size: 35px;">✨</div>
            </div>
            <div class="relative z-10">
                <div class="flex flex-col items-center mb-6">
                    <img :src="collegeLogo" class="w-24 h-24 rounded-full bg-white p-1 border-4 border-sky-400 shadow-lg mb-2 object-contain">
                    <span class="text-yellow-400 font-bold text-sm" x-text="'User: ' + userId"></span>
                </div>
                <p class="text-slate-400 text-xs font-bold mb-2">Navigate Pages:</p>
                <nav class="space-y-2 text-sm font-black">
                    <button @click="currentTab = 'dashboard'; loadData()" :class="currentTab === 'dashboard' ? 'bg-blue-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-blue-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📊 Dashboard</button>
                    <button @click="currentTab = 'mark'; loadData()" :class="currentTab === 'mark' ? 'bg-emerald-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-emerald-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📝 Mark Attendance</button>
                    <button @click="currentTab = 'table'; syncToLive(); loadTableData()" :class="currentTab === 'table' ? 'bg-purple-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-purple-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📅 Attendance Table</button>
                    <button @click="currentTab = 'report'; syncToLive(); loadReportData()" :class="currentTab === 'report' ? 'bg-amber-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-amber-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📑 Monthly Compile Report</button>
                    <button @click="currentTab = 'reset'" :class="currentTab === 'reset' ? 'bg-red-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-red-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🧹 Reset / Clear Attendance</button>
                    <button @click="currentTab = 'students'; loadData()" :class="currentTab === 'students' ? 'bg-cyan-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-cyan-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">👥 Manage Students</button>
                    <button @click="currentTab = 'profile'" :class="currentTab === 'profile' ? 'bg-pink-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-pink-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🏢 College Profile</button>
                </nav>
            </div>
            <button @click="logout" class="bg-emerald-500 hover:bg-emerald-600 py-3 rounded-xl font-black text-center shadow-lg transition relative z-10">🚪 LOGOUT FROM PORTAL</button>
        </div>

        <!-- CONTENT VIEW -->
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
                            <template x-for="m in months">
                                <option :value="m" :selected="m == selectedMonth" x-text="m"></option>
                            </template>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Year</label>
                        <select x-model="selectedYear" @change="loadData()" class="w-full p-2.5 rounded-xl">
                            <template x-for="y in years">
                                <option :value="y" :selected="y == selectedYear" x-text="y"></option>
                            </template>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Subject</label>
                        <select x-model="selectedSubject" @change="loadData()" class="w-full p-2.5 rounded-xl">
                            <template x-for="sub in subjects">
                                <option :value="sub" x-text="sub"></option>
                            </template>
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
            </div>

            <!-- TAB 2: MARK ATTENDANCE -->
            <div x-show="currentTab === 'mark'">
                <div class="grid grid-cols-4 gap-4 mb-6">
                    <select x-model="selectedMonth" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months">
                            <option :value="m" :selected="m == selectedMonth" x-text="m"></option>
                        </template>
                    </select>
                    <select x-model="selectedYear" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years">
                            <option :value="y" :selected="y == selectedYear" x-text="y"></option>
                        </template>
                    </select>
                    <select x-model="selectedSubject" @change="loadData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="sub in subjects">
                            <option :value="sub" x-text="sub"></option>
                        </template>
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
                            <button @click="markStatus('Present')" class="bg-emerald-500 hover:bg-emerald-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition transform active:scale-95">🟢 MARK PRESENT (P)</button>
                            <button @click="markStatus('Absent')" class="bg-red-500 hover:bg-red-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition transform active:scale-95">🔴 MARK ABSENT (A)</button>
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
                <h2 class="text-2xl font-black text-white mb-4">📅 Monthly Register</h2>
                <div class="grid grid-cols-3 gap-4 mb-6">
                    <select x-model="tableMonth" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months">
                            <option :value="m" :selected="m == tableMonth" x-text="m"></option>
                        </template>
                    </select>
                    <select x-model="tableYear" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years">
                            <option :value="y" :selected="y == tableYear" x-text="y"></option>
                        </template>
                    </select>
                    <select x-model="tableSubject" @change="loadTableData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="sub in subjects">
                            <option :value="sub" x-text="sub"></option>
                        </template>
                    </select>
                </div>
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
                            <template x-for="st in tableRows">
                                <tr class="bg-sky-50 hover:bg-sky-200">
                                    <td class="p-3 border sticky left-0 bg-sky-50 z-10" x-text="st.reg_no"></td>
                                    <td class="p-3 border sticky left-28 bg-sky-50 z-10" x-text="st.roll_no"></td>
                                    <td class="p-3 border text-left sticky left-44 bg-sky-50 z-10 truncate" x-text="st.name"></td>
                                    <template x-for="d in tableNumDays">
                                        <td class="border text-xs text-center" :class="st.days[d] === 'P' ? 'bg-emerald-500 text-white font-black' : (st.days[d] === 'A' ? 'bg-red-500 text-white font-black' : '')" x-text="st.days[d]"></td>
                                    </template>
                                    <td class="p-3 border font-black text-blue-800" x-text="st.pct + '%'"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- TAB 4: MONTHLY COMPILE REPORT -->
            <div x-show="currentTab === 'report'">
                <h2 class="text-2xl font-black text-white mb-4">📑 Consolidated Monthly Attendance & Percentage Report</h2>
                <div class="grid grid-cols-2 gap-4 mb-6">
                    <select x-model="reportMonth" @change="loadReportData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="m in months">
                            <option :value="m" :selected="m == reportMonth" x-text="m"></option>
                        </template>
                    </select>
                    <select x-model="reportYear" @change="loadReportData()" class="w-full p-2.5 rounded-xl">
                        <template x-for="y in years">
                            <option :value="y" :selected="y == reportYear" x-text="y"></option>
                        </template>
                    </select>
                </div>
                
                <div class="flex gap-4 mb-6">
                    <a :href="'/api/download_excel/' + userId + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition">📊 DOWNLOAD EXCEL (.XLSX)</a>
                    <a :href="'/api/download_pdf/' + userId + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-red-600 hover:bg-red-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition">📥 DOWNLOAD PDF (.PDF)</a>
                    
                    <!-- NEW CLEAN PDF SHARE BUTTON -->
                    <button @click="shareViaEmail()" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-black py-3 rounded-xl text-center shadow-lg transition flex justify-center items-center gap-2">🔗 SHARE PDF</button>
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
                            <template x-for="st in reportRows">
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

            <!-- TAB 5: RESET / CLEAR ATTENDANCE -->
            <div x-show="currentTab === 'reset'">
                <h2 class="text-2xl font-black text-white mb-4">🧹 Reset / Clear Attendance Logs (Student & Class Level)</h2>
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
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">➕ Add New Student</h3>
                        <form @submit.prevent="addStudent" class="space-y-4">
                            <input type="text" x-model="newStudent.reg_no" placeholder="Registration No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.roll_no" placeholder="Roll No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.name" placeholder="Full Name" required class="w-full p-3 rounded-xl">
                            <button type="submit" class="w-full bg-red-500 hover:bg-red-600 text-white font-black py-3 rounded-xl shadow">Save Student</button>
                        </form>
                    </div>
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">🗑️ Delete Student</h3>
                        <form @submit.prevent="deleteStudent" class="space-y-4">
                            <input type="text" x-model="delRegNo" placeholder="Enter Reg No to Delete" required class="w-full p-3 rounded-xl">
                            <button type="submit" class="w-full bg-red-500 hover:bg-red-600 text-white font-black py-3 rounded-xl shadow">Delete Student</button>
                        </form>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">📥 Bulk Import (Excel/CSV)</h3>
                        <input type="file" id="bulkFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-sky-50">
                        <button @click="bulkImport" class="w-full bg-red-500 hover:bg-red-600 text-white font-black py-3 rounded-xl shadow">Import Data</button>
                    </div>

                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">📸 Upload Photo & Student Profile Details</h3>
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
                            <button type="submit" class="w-full bg-red-500 hover:bg-red-600 text-white font-black py-2.5 rounded-xl shadow">Save Complete Profile & Cloud Photo</button>
                        </form>
                    </div>
                </div>
            </div>

            <!-- TAB 7: COLLEGE PROFILE -->
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
                            <button type="submit" class="w-full bg-red-500 text-white font-black py-3 rounded-xl shadow">Save Metadata</button>
                        </form>
                    </div>
                    <div class="space-y-6">
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">📚 Add Subject</h3>
                            <form @submit.prevent="addSubject" class="space-y-4">
                                <input type="text" x-model="newSubject" placeholder="Subject Name" required class="w-full p-3 rounded-xl">
                                <button type="submit" class="w-full bg-red-500 text-white font-black py-3 rounded-xl shadow">Add Subject</button>
                            </form>
                        </div>
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">🗑️ Delete Subject</h3>
                            <form @submit.prevent="deleteSubject" class="space-y-4">
                                <select x-model="delSubject" class="w-full p-3 rounded-xl">
                                    <option value="">--- Select Subject ---</option>
                                    <template x-for="sub in subjects">
                                        <option :value="sub" x-text="sub"></option>
                                    </template>
                                </select>
                                <button type="submit" class="w-full bg-red-500 text-white font-black py-3 rounded-xl shadow">Delete Subject</button>
                            </form>
                        </div>
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">🖼️ College Logo (Cloud Secured)</h3>
                            <input type="file" id="logoFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-sky-50">
                            <button @click="uploadLogo" class="w-full bg-red-500 hover:bg-red-600 text-white font-black py-3 rounded-xl shadow">Upload Logo to Cloud</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        function erpApp() {
            let now = new Date();
            let mList = ['January','February','March','April','May','June','July','August','September','October','November','December'];
            let yList = ['2025', '2026', '2027'];
            
            let curMonth = mList[now.getMonth()];
            let curYear = String(now.getFullYear());
            let curDate = now.toISOString().split('T')[0];

            return {
                loggedIn: false,
                isLogin: true,
                authForm: { username: '', password: '' },
                authError: '',
                userId: '',
                currentTab: 'dashboard',
                
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

                reportMonth: curMonth,
                reportYear: curYear,
                reportSubjects: [],
                reportRows: [],

                resetScope: 'single',
                resetReg: '',
                resetSubject: 'All Subjects',
                resetDate: curDate,

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
                    let res = await fetch(endpoint, { method: 'POST', body: formData });
                    let data = await res.json();
                    if (res.ok) {
                        this.userId = this.authForm.username;
                        this.loggedIn = true;
                        this.loadData();
                    } else {
                        this.authError = data.detail || "Authentication Failed";
                    }
                },

                async loadData() {
                    let res = await fetch(`/api/data/${this.userId}?month=${this.selectedMonth}&year=${this.selectedYear}&subject=${this.selectedSubject}&target_date=${this.selectedDate}`);
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
                    this.students = data.students;
                    if (this.students.length > 0) this.fetchStudentDetails();
                },

                async loadTableData() {
                    if (!this.tableSubject && this.subjects.length > 0) this.tableSubject = this.subjects[0];
                    let res = await fetch(`/api/attendance_table/${this.userId}?month=${this.tableMonth}&year=${this.tableYear}&subject=${this.tableSubject}`);
                    let data = await res.json();
                    this.tableNumDays = data.num_days;
                    this.tableRows = data.table_data;
                },

                async loadReportData() {
                    let res = await fetch(`/api/compile_report/${this.userId}?month=${this.reportMonth}&year=${this.reportYear}`);
                    let data = await res.json();
                    this.reportSubjects = data.subjects;
                    this.reportRows = data.report;
                },

                // CLEAN PDF SHARE FUNCTION (No custom message, No auto-gmail compose)
                async shareViaEmail() {
                    let pdfUrl = `/api/download_pdf/${this.userId}?month=${this.reportMonth}&year=${this.reportYear}`;
                    let fileName = `Attendance_Report_${this.reportMonth}_${this.reportYear}.pdf`;
                    
                    try {
                        let response = await fetch(pdfUrl);
                        let blob = await response.blob();
                        let file = new File([blob], fileName, {type: "application/pdf"});
                        
                        if (navigator.canShare && navigator.canShare({ files: [file] })) {
                            await navigator.share({
                                files: [file]
                            });
                            return; 
                        } else {
                            throw new Error("Sharing not supported");
                        }
                    } catch(e) {
                        // Fallback: If native share fails, just download the PDF immediately
                        let a = document.createElement('a');
                        a.href = pdfUrl;
                        a.download = fileName;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        alert("PDF Downloaded! You can now manually share it.");
                    }
                },

                get currentStudent() {
                    return this.students[this.currentIndex] || { name: '', reg_no: '', roll_no: '' };
                },

                async fetchStudentDetails() {
                    let reg = this.currentStudent.reg_no;
                    if (!reg) return;
                    let res = await fetch(`/api/student_details/${this.userId}/${reg}`);
                    let data = await res.json();
                    this.currentStudentDetails = data;
                    this.currentStudentPhoto = data.photo_data;
                },

                async markStatus(status) {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('student_id', this.currentStudent.id);
                    formData.append('subject', this.selectedSubject);
                    formData.append('date_str', this.selectedDate);
                    formData.append('status', status);
                    
                    if (this.currentIndex < this.students.length - 1) {
                        this.currentIndex++;
                        this.fetchStudentDetails();
                    }

                    await fetch('/api/mark_attendance', { method: 'POST', body: formData });
                    this.loadData();
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
                        alert('Student added successfully!');
                        this.newStudent = { reg_no: '', roll_no: '', name: '' };
                        this.loadData();
                    }
                },

                async deleteStudent() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.delRegNo);
                    let res = await fetch('/api/delete_student', { method: 'POST', body: formData });
                    if (res.ok) {
                        alert('Student deleted successfully!');
                        this.delRegNo = '';
                        this.loadData();
                    }
                },

                async bulkImport() {
                    let fileInput = document.getElementById('bulkFile');
                    if (fileInput.files.length === 0) { alert('Please select a CSV or Excel file.'); return; }
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    let res = await fetch('/api/bulk_import', { method: 'POST', body: formData });
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
                    }
                },

                async addSubject() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.newSubject);
                    let res = await fetch('/api/add_subject', { method: 'POST', body: formData });
                    if (res.ok) {
                        alert('Subject added successfully!');
                        this.newSubject = '';
                        this.loadData();
                    }
                },

                async deleteSubject() {
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.delSubject);
                    let res = await fetch('/api/delete_subject', { method: 'POST', body: formData });
                    if (res.ok) {
                        alert('Subject deleted successfully!');
                        this.delSubject = '';
                        this.loadData();
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
                        alert('Logo uploaded successfully!');
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
                        alert('College metadata updated successfully!');
                        this.loadData();
                    }
                },

                async saveStudentProfile() {
                    if (!this.profileReg) { alert('Please select a student.'); return; }
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
                        alert('Student profile & photo saved successfully!');
                        this.fetchStudentDetails();
                    }
                },

                logout() {
                    this.loggedIn = false;
                    this.userId = '';
                }
            }
        }
    </script>
</body>
</html>
"""