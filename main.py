import os
import io
import re
import base64
import calendar
from datetime import datetime, date
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from sqlalchemy import create_engine, text

app = FastAPI(title="ISM Attendance ERP - Final Full Edition")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    try:
        import streamlit as st
        DATABASE_URL = st.secrets["DATABASE_URL"]
    except Exception:
        DATABASE_URL = "postgresql://postgres.parhsaqmmmiyojwkhsrn:%40fr3rdEyp.%2B%25ug%3D@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

from sqlalchemy.pool import NullPool
engine = create_engine(DATABASE_URL, pool_pre_ping=True, poolclass=NullPool)

def init_master_db():
    try:
        with engine.begin() as conn:
            conn.execute(text('''
                CREATE TABLE IF NOT EXISTS master_users (
                    username TEXT PRIMARY KEY, 
                    password TEXT
                )
            '''))
    except Exception as e:
        print("Master DB init error:", e)

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
        except Exception:
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
            created_at TEXT DEFAULT '',
            document_data TEXT DEFAULT ''
        )'''))

        try:
            conn.execute(text(f'ALTER TABLE {t_details} ADD COLUMN IF NOT EXISTS photo_data TEXT'))
        except Exception:
            pass

        try:
            conn.execute(text(f'ALTER TABLE {t_leaves} ADD COLUMN IF NOT EXISTS subject TEXT'))
            conn.execute(text(f'ALTER TABLE {t_leaves} ADD COLUMN IF NOT EXISTS faculty_remark TEXT DEFAULT \'\''))
            conn.execute(text(f'ALTER TABLE {t_leaves} ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT \'\''))
            conn.execute(text(f'ALTER TABLE {t_leaves} ADD COLUMN IF NOT EXISTS document_data TEXT DEFAULT \'\''))
        except Exception:
            pass

        res = conn.execute(text(f"SELECT COUNT(*) FROM {t_subjects}")).fetchone()[0]
        if res == 0:
            for sub in ['SAD', 'PST&PC', 'NT', 'BE', 'OS&UNIX LAB', 'PROG IN C LAB']:
                conn.execute(text(f"INSERT INTO {t_subjects} (subject_name) VALUES (:sub) ON CONFLICT DO NOTHING"), {"sub": sub})

# ==========================================
# AUTHENTICATION APIS
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
        return {"success": True, "message": "Portal registered successfully! You can now login."}
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
                    init_tenant_db(f_id)
                    return {"success": True, "faculty_id": f_id, "reg_no": r_no, "name": st[1]}
            except Exception:
                continue

    raise HTTPException(status_code=400, detail="Student not found. Please verify your Registration Number and Name.")

@app.get("/api/student_dashboard_data/{faculty_id}/{reg_no}")
def get_student_dashboard_data(faculty_id: str, reg_no: str):
    init_tenant_db(faculty_id)
    safe_uid = get_safe_prefix(faculty_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"
    t_settings = f"{safe_uid}_settings"
    t_leaves = f"{safe_uid}_leaves"

    with engine.begin() as conn:
        st = conn.execute(text(f"SELECT id, name, roll_no FROM {t_students} WHERE LOWER(reg_no)=LOWER(:r)"), {"r": reg_no.strip()}).fetchone()
        if not st: 
            return {"error": "Student record not found in class"}
        st_id, st_name, st_roll = st[0], st[1], st[2]

        sub_rows = conn.execute(text(f"SELECT id, subject_name FROM {t_subjects} ORDER BY subject_name")).fetchall()
        sub_map = {r[1]: r[0] for r in sub_rows}

        sub_total_classes = {sub: conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid"), {"sid": sid}).fetchone()[0] or 0 for sub, sid in sub_map.items()}
        att_rows = conn.execute(text(f"SELECT subject_id, COUNT(*) FROM {t_attendance} WHERE student_id=:sid AND status='Present' GROUP BY subject_id"), {"sid": st_id}).fetchall()
        present_map = {r[0]: r[1] for r in att_rows}

        summary = []
        tot_p_all = 0
        tot_c_all = 0
        for sub, sid in sub_map.items():
            tot_c = sub_total_classes.get(sub, 0)
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

        leaves = []
        try:
            leave_rows = conn.execute(text(f"""
                SELECT id, leave_type, COALESCE(subject, 'All Subjects'), from_date, to_date, reason, status, faculty_remark, created_at, document_data
                FROM {t_leaves}
                WHERE LOWER(reg_no) = LOWER(:r)
                ORDER BY id DESC
            """), {"r": reg_no.strip()}).fetchall()

            leaves = [{
                "id": r[0], "leave_type": r[1], "subject": r[2], "from_date": r[3],
                "to_date": r[4], "reason": r[5], "status": r[6], "faculty_remark": r[7] or '', "created_at": r[8] or '', "document_data": r[9] or ''
            } for r in leave_rows]
        except Exception:
            leaves = []

        def get_cfg(k, def_v):
            try:
                res = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key=:k"), {"k": k}).fetchone()
                return res[0] if res and res[0] else def_v
            except Exception:
                return def_v

        college_name = get_cfg('college_name', 'INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)')
        subtitle = get_cfg('app_subtitle', 'ATTENDANCE MANAGEMENT SYSTEM')
        course = get_cfg('course_name', 'BCA')
        sec = get_cfg('section_name', 'Semester 1')
        college_logo = get_cfg('college_logo', 'https://i.ibb.co/3s68K1v/tree-logo.png')

        return {
            "student": {"name": st_name, "reg_no": reg_no, "roll_no": st_roll},
            "faculty_id": faculty_id,
            "overall_pct": overall_pct,
            "summary": summary,
            "history": history,
            "leaves": leaves,
            "college_name": college_name,
            "course": f"{subtitle} | {course} | {sec}",
            "logo": college_logo
        }

# ==========================================
# LEAVE SYSTEM APIS
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
    reason: str = Form(...),
    file: UploadFile = File(None)
):
    try:
        encoded_doc = ""
        if file and file.filename:
            contents = await file.read()
            ext = file.filename.split('.')[-1].lower()
            mime_type = file.content_type or f"image/{ext}"
            encoded_doc = f"data:{mime_type};base64,{base64.b64encode(contents).decode('utf-8')}"

        init_tenant_db(faculty_id)
        safe_uid = get_safe_prefix(faculty_id)
        t_leaves = f"{safe_uid}_leaves"
        c_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {t_leaves} (reg_no, student_name, leave_type, subject, from_date, to_date, reason, status, created_at, document_data)
                VALUES (:r, :sn, :lt, :sub, :fd, :td, :re, 'Pending', :ca, :doc)
            """), {
                "r": reg_no.strip(),
                "sn": student_name.strip(),
                "lt": leave_type.strip(),
                "sub": subject.strip(),
                "fd": from_date.strip(),
                "td": to_date.strip(),
                "re": reason.strip(),
                "ca": c_time,
                "doc": encoded_doc
            })
        return {"success": True, "message": "Leave application sent to your faculty successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to apply leave: " + str(e))

@app.get("/api/leaves/{user_id}")
def get_faculty_leaves(user_id: str):
    init_tenant_db(user_id)
    safe_uid = get_safe_prefix(user_id)
    t_leaves = f"{safe_uid}_leaves"
    with engine.begin() as conn:
        rows = conn.execute(text(f"""
            SELECT id, reg_no, student_name, leave_type, COALESCE(subject, 'All Subjects'), from_date, to_date, reason, status, faculty_remark, created_at, document_data
            FROM {t_leaves}
            ORDER BY id DESC
        """)).fetchall()

        leaves = [{
            "id": r[0], "reg_no": r[1], "student_name": r[2], "leave_type": r[3],
            "subject": r[4], "from_date": r[5], "to_date": r[6], "reason": r[7],
            "status": r[8], "faculty_remark": r[9] or '', "created_at": r[10] or '', "document_data": r[11] or ''
        } for r in rows]
        return {"leaves": leaves}

@app.post("/api/update_leave_status")
def update_leave_status(
    user_id: str = Form(...),
    leave_id: int = Form(...),
    status: str = Form(...),
    faculty_remark: str = Form("")
):
    try:
        safe_uid = get_safe_prefix(user_id)
        t_leaves = f"{safe_uid}_leaves"
        with engine.begin() as conn:
            conn.execute(text(f"""
                UPDATE {t_leaves}
                SET status = :s, faculty_remark = :r
                WHERE id = :lid
            """), {"s": status, "r": faculty_remark.strip(), "lid": leave_id})
        return {"success": True, "message": f"Leave application marked as {status}!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Server Error: " + str(e))

# ==========================================
# FACULTY CORE APIS
# ==========================================

@app.get("/api/data/{user_id}")
def get_dashboard_data(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE", target_date: str = "2026-07-25"):
    init_tenant_db(user_id)
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

        defaulters = []
        if sub_id and tc_count > 0:
            att_counts = conn.execute(text(f"""
                SELECT student_id, COUNT(*) FROM {t_attendance}
                WHERE subject_id = :sid AND date LIKE :d AND status = 'Present'
                GROUP BY student_id
            """), {"sid": sub_id, "d": date_pattern}).fetchall()
            pres_dict = {r[0]: r[1] for r in att_counts}

            for s in students:
                p_cnt = pres_dict.get(s[0], 0)
                pct = round((p_cnt / tc_count) * 100)
                if pct < 75:
                    defaulters.append({
                        "id": s[0],
                        "reg_no": s[1],
                        "roll_no": s[2],
                        "name": s[3],
                        "present": p_cnt,
                        "total": tc_count,
                        "pct": pct
                    })

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
        "defaulters": defaulters,
        "college_name": c_name,
        "app_subtitle": c_sub,
        "course_name": c_course,
        "section_name": c_sec,
        "college_logo": logo_url
    }

# ==========================================
# ABSENTEES LIST API & PDF GENERATOR
# ==========================================
@app.get("/api/absentees/{user_id}")
def get_absentees_list(user_id: str, subject: str = "BE", date_str: str = ""):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    with engine.begin() as conn:
        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        if not sub_id_res:
            return {"absentees": []}
        sub_id = sub_id_res[0]

        absentees_raw = conn.execute(text(f"""
            SELECT s.reg_no, s.roll_no, s.name
            FROM {t_students} s
            JOIN {t_attendance} a ON s.id = a.student_id
            WHERE a.subject_id = :sid AND a.date = :dt AND a.status = 'Absent'
        """), {"sid": sub_id, "dt": date_str}).fetchall()

        absentees = [{"reg_no": r[0], "roll_no": r[1], "name": r[2]} for r in absentees_raw]
        
        def safe_roll(x):
            try:
                return int(''.join(filter(str.isdigit, str(x["roll_no"]))))
            except:
                return str(x["roll_no"])
        absentees = sorted(absentees, key=safe_roll)
        
        return {"absentees": absentees}

@app.get("/api/download_absentees_pdf/{user_id}")
def download_absentees_pdf(user_id: str, subject: str = "BE", date_str: str = ""):
    try:
        from reportlab.lib.pagesizes import A4
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

    with engine.begin() as conn:
        c_name = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key='college_name'")).fetchone()
        college_name = c_name[0] if c_name else "INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)"

        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None

        absentees = []
        if sub_id and date_str:
            absentees_raw = conn.execute(text(f"""
                SELECT s.roll_no, s.reg_no, s.name
                FROM {t_students} s
                JOIN {t_attendance} a ON s.id = a.student_id
                WHERE a.subject_id = :sid AND a.date = :dt AND a.status = 'Absent'
            """), {"sid": sub_id, "dt": date_str}).fetchall()
            
            def safe_roll(x):
                try:
                    return int(''.join(filter(str.isdigit, str(x[0]))))
                except:
                    return str(x[0])
            absentees = sorted(absentees_raw, key=safe_roll)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('HeaderTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor('#0f172a'), alignment=1)
    sub_style = ParagraphStyle('HeaderSub', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#dc2626'), alignment=1)
    cell_name_style = ParagraphStyle('CellName', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12, textColor=colors.HexColor('#0f172a'))
    cell_center_style = ParagraphStyle('CellCenter', parent=styles['Normal'], fontName='Helvetica', fontSize=10, alignment=1)

    headers = ["Sl No", "Roll No", "Reg No", "Student Name"]
    table_data = [[
        Paragraph(f"<b>{h}</b>", ParagraphStyle('Hdr', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=colors.whitesmoke, alignment=1)) for h in headers
    ]]

    for idx, d in enumerate(absentees, start=1):
        table_data.append([
            Paragraph(str(idx), cell_center_style),
            Paragraph(str(d[0]), cell_center_style),
            Paragraph(str(d[1]), cell_center_style),
            Paragraph(str(d[2]), cell_name_style)
        ])

    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = [
        Paragraph(college_name, title_style),
        Paragraph(f"DAILY ABSENTEES REPORT — {subject} ({date_str})", sub_style),
        Spacer(1, 15)
    ]

    col_widths = [50, 80, 100, 270]

    t = RLTable(table_data, colWidths=col_widths, repeatRows=1)
    
    if not absentees:
        table_data.append([Paragraph("No Absentees / Data Not Found for this date", cell_center_style), "", "", ""])
        style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#991b1b')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#fca5a5')),
            ('SPAN', (0, 1), (3, 1))
        ])
    else:
        style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#991b1b')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#fca5a5')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#fef2f2')])
        ])
        
    t.setStyle(style)
    elements.append(t)
    doc.build(elements)
    pdf_buf.seek(0)
    return StreamingResponse(pdf_buf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Absentees_{subject}_{date_str}.pdf"})


@app.get("/api/download_defaulters_excel/{user_id}")
def download_defaulters_excel(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE"):
    safe_uid = get_safe_prefix(user_id)
    t_students = f"{safe_uid}_students"
    t_subjects = f"{safe_uid}_subjects"
    t_attendance = f"{safe_uid}_attendance"

    month_num = list(calendar.month_name).index(month) if month in list(calendar.month_name) else 7
    date_pattern = f"{year}-{month_num:02d}-%"

    with engine.begin() as conn:
        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)

        defaulters_data = []
        if sub_id:
            tc_count = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            if tc_count > 0:
                att_counts = conn.execute(text(f"""
                    SELECT student_id, COUNT(*) FROM {t_attendance}
                    WHERE subject_id = :sid AND date LIKE :d AND status = 'Present'
                    GROUP BY student_id
                """), {"sid": sub_id, "d": date_pattern}).fetchall()
                pres_dict = {r[0]: r[1] for r in att_counts}

                for s in students:
                    p_cnt = pres_dict.get(s[0], 0)
                    pct = round((p_cnt / tc_count) * 100)
                    if pct < 75:
                        defaulters_data.append({
                            "Registration No": s[1],
                            "Roll No": s[2],
                            "Student Name": s[3],
                            "Subject": subject,
                            "Present Classes": p_cnt,
                            "Total Classes": tc_count,
                            "Attendance %": f"{pct}%",
                            "Status": "Defaulter (< 75%)"
                        })

    df = pd.DataFrame(defaulters_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="Defaulters")
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=Defaulters_{subject}_{month}_{year}.xlsx"})

@app.get("/api/download_defaulters_pdf/{user_id}")
def download_defaulters_pdf(user_id: str, month: str = "July", year: int = 2026, subject: str = "BE"):
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
        c_name = conn.execute(text(f"SELECT value FROM {t_settings} WHERE key='college_name'")).fetchone()
        college_name = c_name[0] if c_name else "INTERNATIONAL SCHOOL OF MANAGEMENT (ISM)"

        sub_id_res = conn.execute(text(f"SELECT id FROM {t_subjects} WHERE subject_name=:s"), {"s": subject}).fetchone()
        sub_id = sub_id_res[0] if sub_id_res else None
        students_raw = conn.execute(text(f"SELECT id, reg_no, roll_no, name FROM {t_students}")).fetchall()
        students = sort_students_safely(students_raw)

        defaulters = []
        if sub_id:
            tc_count = conn.execute(text(f"SELECT COUNT(DISTINCT date) FROM {t_attendance} WHERE subject_id=:sid AND date LIKE :d"), {"sid": sub_id, "d": date_pattern}).fetchone()[0] or 0
            if tc_count > 0:
                att_counts = conn.execute(text(f"""
                    SELECT student_id, COUNT(*) FROM {t_attendance}
                    WHERE subject_id = :sid AND date LIKE :d AND status = 'Present'
                    GROUP BY student_id
                """), {"sid": sub_id, "d": date_pattern}).fetchall()
                pres_dict = {r[0]: r[1] for r in att_counts}

                for s in students:
                    p_cnt = pres_dict.get(s[0], 0)
                    pct = round((p_cnt / tc_count) * 100)
                    if pct < 75:
                        defaulters.append((s[2], s[1], s[3], f"{p_cnt} / {tc_count}", f"{pct}%"))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('HeaderTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor('#0f172a'), alignment=1)
    sub_style = ParagraphStyle('HeaderSub', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#dc2626'), alignment=1)
    cell_name_style = ParagraphStyle('CellName', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11, textColor=colors.HexColor('#0f172a'))
    cell_center_style = ParagraphStyle('CellCenter', parent=styles['Normal'], fontName='Helvetica', fontSize=9, alignment=1)

    headers = ["Roll No", "Reg No", "Student Name", "Present / Total", "Attendance %", "Status"]
    table_data = [[
        Paragraph(f"<b>{h}</b>", ParagraphStyle('Hdr', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, textColor=colors.whitesmoke, alignment=1)) for h in headers
    ]]

    for d in defaulters:
        table_data.append([
            Paragraph(str(d[0]), cell_center_style),
            Paragraph(str(d[1]), cell_center_style),
            Paragraph(str(d[2]), cell_name_style),
            Paragraph(str(d[3]), cell_center_style),
            Paragraph(f"<font color='#dc2626'><b>{d[4]}</b></font>", cell_center_style),
            Paragraph("<font color='#dc2626'><b>Shortage (<75%)</b></font>", cell_center_style)
        ])

    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=landscape(A4), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    elements = [
        Paragraph(college_name, title_style),
        Paragraph(f"ATTENDANCE DEFAULTERS LIST (< 75%) — {subject} ({month.upper()} {year})", sub_style),
        Spacer(1, 12)
    ]

    col_widths = [60, 90, 310, 110, 100, 130] 

    t = RLTable(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#991b1b')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#fca5a5')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#fef2f2')])
    ]))
    elements.append(t)
    doc.build(elements)
    pdf_buf.seek(0)
    return StreamingResponse(pdf_buf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Defaulters_{subject}_{month}_{year}.pdf"})

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
                except Exception:
                    pass

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
                except Exception:
                    pass

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
            row = {"Registration No": reg, "Roll No": roll, "Student Name": name}
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

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('HeaderTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor('#0f172a'), alignment=1)
        sub_style = ParagraphStyle('HeaderSub', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#d97706'), alignment=1)
        cell_name_style = ParagraphStyle('CellName', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#0f172a'))
        cell_center_style = ParagraphStyle('CellCenter', parent=styles['Normal'], fontName='Helvetica', fontSize=8, leading=10, alignment=1)
        hdr_style = ParagraphStyle('Hdr', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.whitesmoke, alignment=1)

        headers = ["Roll", "Reg No", "Student Name"] + subjects + ["Overall %"]
        table_data = [[Paragraph(f"<b>{h}</b>", hdr_style) for h in headers]]

        for st in students:
            st_id, reg, roll, name = st
            row = [
                Paragraph(str(roll), cell_center_style),
                Paragraph(str(reg), cell_center_style),
                Paragraph(str(name), cell_name_style)
            ]
            tot_p_all, tot_c_all = 0, 0
            for sub, sub_id in sub_map.items():
                tot_c = sub_total_classes.get(sub, 0)
                tot_p = present_map.get((st_id, sub_id), 0)
                tot_p_all += tot_p
                tot_c_all += tot_c
                pct = round((tot_p / tot_c * 100)) if tot_c > 0 else 0
                row.append(Paragraph(f"{tot_p}/{tot_c}<br/>({pct}%)", cell_center_style))
            overall_pct = round((tot_p_all / tot_c_all * 100), 1) if tot_c_all > 0 else 0
            row.append(Paragraph(f"<b>{overall_pct}%</b>", cell_center_style))
            table_data.append(row)

    total_table_width = 800
    num_subs = len(subjects)
    fixed_roll_w = 40
    fixed_reg_w = 80
    fixed_pct_w = 55
    sub_col_w = max(48, min(70, int(380 / max(1, num_subs))))
    name_col_w = max(180, total_table_width - (fixed_roll_w + fixed_reg_w + fixed_pct_w + (sub_col_w * num_subs)))

    col_widths = [fixed_roll_w, fixed_reg_w, name_col_w] + [sub_col_w] * num_subs + [fixed_pct_w]

    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=landscape(A4), rightMargin=15, leftMargin=15, topMargin=15, bottomMargin=15)
    elements = [
        Paragraph(college_name, title_style),
        Paragraph(f"CONSOLIDATED ATTENDANCE REPORT — {month.upper()} {year}", sub_style),
        Spacer(1, 10)
    ]

    t = RLTable(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a8a')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
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
                        conn.execute(text(f"DELETE FROM {t_attendance} WHERE date=:dt AND subject_id=:subid"), {"subid": sub_res[0]})
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
                except Exception:
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
        except Exception:
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
        return {"success": True, "logo_url": b64_val, "message": "College logo updated and stored securely!"}
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
# MANIFEST ENDPOINT (FOR PWA/INSTALL)
# ==========================================
@app.get("/manifest.json")
def get_manifest():
    if os.path.exists("manifest.json"):
        return FileResponse("manifest.json", media_type="application/json")
    return {"error": "manifest.json file not found in root directory"}


# ==========================================
# FRONTEND UI (COMPLETE & UNIFIED)
# ==========================================

@app.get("/", response_class=HTMLResponse)
def home():
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ISM Attendance ERP - Final Full Edition</title>
    
    <!-- PWA Manifest Link -->
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#1e3a8a">
    <link rel="apple-touch-icon" href="https://i.ibb.co/3s68K1v/tree-logo.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black">

    <script src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" defer></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: radial-gradient(circle at 50% 30%, #1e3a8a 0%, #0f172a 60%, #020617 100%); min-height: 100vh; color: white; overflow-x: hidden; position: relative; }
        .anim-container { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; pointer-events: none; overflow: hidden; z-index: 0; }
        .floating-icon { position: absolute; bottom: -150px; opacity: 0.45; animation: floatUp 15s infinite linear; filter: drop-shadow(0 0 15px rgba(56, 189, 248, 0.9)); }
        @keyframes floatUp { 0% { transform: translateY(0) rotate(0deg) scale(0.9); opacity: 0; } 20% { opacity: 0.5; } 80% { opacity: 0.5; } 100% { transform: translateY(-115vh) rotate(360deg) scale(1.2); opacity: 0; } }
        .glass-card { background: rgba(15, 23, 42, 0.75) !important; backdrop-filter: blur(8px); border: 2px solid rgba(56, 189, 248, 0.3); }
        input, select, textarea { background-color: #e0f2fe !important; color: #0f172a !important; border: 2px solid #38bdf8 !important; font-weight: 800 !important; }
        input::placeholder, textarea::placeholder { color: #64748b !important; }
        
        /* Fixed Grid Table styling */
        .math-grid-table { border-collapse: separate !important; border-spacing: 0 !important; }
        .math-grid-table th, .math-grid-table td { border: 1px solid #38bdf8 !important; }
        
        .col-reg { width: 110px !important; min-width: 110px !important; max-width: 110px !important; left: 0px !important; }
        .col-roll { width: 60px !important; min-width: 60px !important; max-width: 60px !important; left: 110px !important; }
        .col-name { width: 260px !important; min-width: 260px !important; max-width: 260px !important; left: 170px !important; text-align: left !important; padding-left: 10px !important; }
        
        .day-cell { width: 34px !important; min-width: 34px !important; max-width: 34px !important; height: 34px !important; padding: 0 !important; font-size: 11px !important; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.6); }
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

    <!-- LOGIN SCREEN -->
    <div x-show="!loggedIn" class="flex items-center justify-center min-h-screen p-6 relative z-10">
        <div class="glass-card p-10 rounded-3xl shadow-2xl w-full max-w-5xl grid grid-cols-1 md:grid-cols-2 gap-10 items-center">
            <div>
                <div class="inline-block bg-sky-950/80 border border-sky-400/40 px-3 py-1 rounded-full text-xs font-bold text-sky-400 mb-4 shadow">⚡ ENTERPRISE CLOUD PORTAL</div>
                <h1 class="text-4xl font-black text-white mb-2">🎓 ISM PATNA</h1>
                <h3 class="text-lg font-bold text-amber-400 mb-4">ATTENDANCE ERP SYSTEM</h3>
                <p class="text-slate-300 text-sm leading-relaxed mb-6">Multi-Tenant Attendance & Student Leave Management ERP System for ISM Patna.</p>
                <div class="space-y-4">
                    <div class="bg-sky-950/60 border border-sky-500/40 p-4 rounded-xl flex items-center gap-4">
                        <div class="text-3xl">👨‍🏫</div>
                        <div>
                            <p class="text-sky-300 font-bold text-sm">Faculty Portal</p>
                            <p class="text-slate-400 text-xs">Mark attendance, check defaulters (< 75%), manage records, and approve leaves.</p>
                        </div>
                    </div>
                    <div class="bg-emerald-950/60 border border-emerald-500/40 p-4 rounded-xl flex items-center gap-4">
                        <div class="text-3xl">🎓</div>
                        <div>
                            <p class="text-emerald-300 font-bold text-sm">Student Portal</p>
                            <p class="text-slate-400 text-xs">Check subject-wise status, register history, and compose leave applications.</p>
                        </div>
                    </div>
                </div>
            </div>

            <div class="bg-slate-900/90 p-8 rounded-2xl border border-sky-400/30 shadow-2xl">
                <div class="flex gap-2 mb-6 bg-slate-950 p-1.5 rounded-xl border border-slate-700">
                    <button @click="authRole = 'faculty'; isLogin = true" :class="authRole === 'faculty' ? 'bg-blue-600 text-white shadow' : 'text-slate-400'" class="flex-1 py-3 font-black rounded-lg transition text-sm">👨‍🏫 FACULTY</button>
                    <button @click="authRole = 'student'" :class="authRole === 'student' ? 'bg-emerald-600 text-white shadow' : 'text-slate-400'" class="flex-1 py-3 font-black rounded-lg transition text-sm">🎓 STUDENT</button>
                </div>

                <!-- FACULTY FORM -->
                <div x-show="authRole === 'faculty'">
                    <div class="flex gap-2 mb-6 bg-slate-800 p-1 rounded-xl">
                        <button @click="isLogin = true" :class="isLogin ? 'bg-sky-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-1.5 font-bold rounded-lg transition text-xs">🔐 Login</button>
                        <button @click="isLogin = false" :class="!isLogin ? 'bg-sky-500 text-white shadow' : 'text-slate-400'" class="flex-1 py-1.5 font-bold rounded-lg transition text-xs">📄 Register Class</button>
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
                        <button type="submit" :disabled="isProcessing" class="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow-lg transition text-sm flex justify-center items-center gap-2">
                            <span x-show="isProcessing">⏳ Please wait...</span>
                            <span x-show="!isProcessing" x-text="isLogin ? 'FACULTY LOGIN' : 'CREATE CLASS PORTAL'"></span>
                        </button>
                    </form>
                </div>

                <!-- STUDENT FORM -->
                <div x-show="authRole === 'student'">
                    <p class="text-emerald-400 text-xs font-bold mb-4 text-center">Enter your registered details to view reports</p>
                    <form @submit.prevent="submitStudentAuth" class="space-y-4">
                        <div>
                            <label class="block text-emerald-400 font-bold text-xs mb-1">Registration No.</label>
                            <input type="text" x-model="studentForm.reg_no" placeholder="Enter your Reg No." required class="w-full p-3 rounded-xl text-sm">
                        </div>
                        <div>
                            <label class="block text-emerald-400 font-bold text-xs mb-1">Student Full Name</label>
                            <input type="text" x-model="studentForm.name" placeholder="Enter your full name" required class="w-full p-3 rounded-xl text-sm">
                        </div>
                        <button type="submit" :disabled="isProcessing" class="w-full bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow-lg transition text-sm flex justify-center items-center gap-2">
                            <span x-show="isProcessing">⏳ Loading Profile...</span>
                            <span x-show="!isProcessing">ACCESS STUDENT DASHBOARD</span>
                        </button>
                    </form>
                </div>
                <p x-text="authError" class="text-red-400 text-center text-xs font-bold mt-4"></p>
            </div>
        </div>
    </div>

    <!-- FACULTY DASHBOARD -->
    <div x-show="loggedIn && userRole === 'faculty'" class="flex h-screen overflow-hidden relative z-10" style="display: none;">
        <div class="w-72 bg-gradient-to-b from-blue-950 via-slate-950 to-slate-950 border-r-2 border-sky-400/50 flex flex-col justify-between p-4 shadow-2xl relative overflow-hidden">
            <div class="relative z-10 overflow-y-auto pr-1 custom-scrollbar">
                <div class="flex flex-col items-center mb-6">
                    <img :src="collegeLogo" class="w-24 h-24 rounded-full bg-white p-1 border-4 border-sky-400 shadow-lg mb-2 object-contain">
                    <span class="text-yellow-400 font-bold text-sm" x-text="'User: ' + userId"></span>
                </div>
                <p class="text-slate-400 text-xs font-bold mb-2">Navigate Pages:</p>
                <nav class="space-y-2 text-sm font-black">
                    <button @click="currentTab = 'dashboard'; loadData()" :class="currentTab === 'dashboard' ? 'bg-blue-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-blue-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📊 Dashboard</button>
                    <button @click="currentTab = 'mark'; loadData()" :class="currentTab === 'mark' ? 'bg-emerald-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-emerald-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📝 Mark Attendance</button>
                    <button @click="currentTab = 'table'; syncToLive(); loadTableData()" :class="currentTab === 'table' ? 'bg-purple-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-purple-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📅 Attendance Table</button>
                    
                    <button @click="currentTab = 'absentees'; loadAbsentees()" :class="currentTab === 'absentees' ? 'bg-rose-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-rose-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🔴 Absentees List</button>
                    
                    <button @click="currentTab = 'report'; syncToLive(); loadReportData()" :class="currentTab === 'report' ? 'bg-amber-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-amber-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">📑 Monthly Compile Report</button>
                    <button @click="currentTab = 'leaves'; loadFacultyLeaves()" :class="currentTab === 'leaves' ? 'bg-indigo-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-indigo-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center justify-between">
                        <span class="flex items-center gap-2">✉️ Leave Requests</span>
                        <span x-show="pendingLeavesCount > 0" class="bg-red-500 text-white text-xs px-2 py-0.5 rounded-full" x-text="pendingLeavesCount"></span>
                    </button>
                    <button @click="currentTab = 'reset'" :class="currentTab === 'reset' ? 'bg-red-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-red-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🧹 Reset / Clear Logs</button>
                    <button @click="currentTab = 'students'; loadData()" :class="currentTab === 'students' ? 'bg-cyan-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-cyan-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">👥 Manage Students</button>
                    <button @click="currentTab = 'profile'" :class="currentTab === 'profile' ? 'bg-pink-600 border-2 border-yellow-300 shadow-lg scale-105' : 'bg-pink-900/80'" class="w-full text-left py-2.5 px-4 rounded-xl transition flex items-center gap-2">🏢 College Profile</button>
                </nav>
            </div>
            <button @click="logout" class="bg-emerald-500 hover:bg-emerald-600 py-3 rounded-xl font-black text-center shadow-lg transition relative z-10 mt-4">🚪 LOGOUT</button>
        </div>

        <!-- MAIN VIEW -->
        <div class="flex-1 flex flex-col overflow-y-auto p-6 custom-scrollbar">
            <div class="glass-card p-4 rounded-2xl shadow-xl flex items-center gap-4 mb-6 border-b-4 border-amber-500">
                <img :src="collegeLogo" class="w-16 h-16 object-contain bg-white rounded-lg p-1">
                <div>
                    <h1 class="text-xl font-black text-white" x-text="collegeName"></h1>
                    <p class="text-yellow-400 font-bold text-xs mt-1" x-text="appSubtitle + ' | ' + courseName + ' | ' + sectionName"></p>
                </div>
            </div>

            <!-- DASHBOARD TAB -->
            <div x-show="currentTab === 'dashboard'">
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

                <div class="grid grid-cols-4 gap-6 mb-8">
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

                <!-- DEFAULTERS LIST -->
                <div class="glass-card p-6 rounded-3xl border-2 border-red-500/50 shadow-2xl">
                    <div class="flex justify-between items-center mb-4 pb-3 border-b border-red-500/30">
                        <div>
                            <h3 class="text-xl font-black text-red-400 flex items-center gap-2">⚠️ Defaulters List (< 75% Attendance)</h3>
                            <p class="text-xs text-slate-300 mt-1">Students below 75% attendance in <b class="text-yellow-400" x-text="selectedSubject"></b> for <b class="text-yellow-400" x-text="selectedMonth + ' ' + selectedYear"></b>.</p>
                        </div>
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="bg-red-950 text-red-400 font-bold border border-red-500/50 px-3 py-1 rounded-full text-xs" x-text="defaultersList.length + ' Students Shortage'"></span>
                            <a :href="'/api/download_defaulters_excel/' + userId + '?month=' + selectedMonth + '&year=' + selectedYear + '&subject=' + selectedSubject" class="bg-emerald-600 hover:bg-emerald-700 text-white font-black text-xs py-2 px-3 rounded-xl shadow transition">📊 Export Excel</a>
                            <a :href="'/api/download_defaulters_pdf/' + userId + '?month=' + selectedMonth + '&year=' + selectedYear + '&subject=' + selectedSubject" class="bg-red-600 hover:bg-red-700 text-white font-black text-xs py-2 px-3 rounded-xl shadow transition">📥 Export PDF</a>
                            <button @click="shareDefaultersPdf()" class="bg-blue-600 hover:bg-blue-700 text-white font-black text-xs py-2 px-3 rounded-xl shadow transition">🔗 Share PDF</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto bg-slate-900/80 rounded-2xl border border-red-500/30">
                        <table class="w-full text-sm text-center">
                            <thead>
                                <tr class="bg-red-950/80 text-red-300 font-bold border-b border-red-500/40">
                                    <th class="p-3">Roll No</th>
                                    <th class="p-3">Reg No</th>
                                    <th class="p-3 text-left">Student Name</th>
                                    <th class="p-3">Present / Total</th>
                                    <th class="p-3">Attendance %</th>
                                    <th class="p-3">Status</th>
                                </tr>
                            </thead>
                            <tbody class="text-slate-200">
                                <template x-for="st in defaultersList">
                                    <tr class="border-b border-slate-800 hover:bg-red-950/20 font-semibold">
                                        <td class="p-3 font-mono" x-text="st.roll_no"></td>
                                        <td class="p-3 text-sky-400" x-text="st.reg_no"></td>
                                        <td class="p-3 text-left font-bold text-white" x-text="st.name"></td>
                                        <td class="p-3" x-text="st.present + ' / ' + st.total"></td>
                                        <td class="p-3 font-black text-red-400" x-text="st.pct + '%'"></td>
                                        <td class="p-3">
                                            <span class="bg-red-900/60 text-red-300 border border-red-500/50 text-[11px] px-2.5 py-0.5 rounded-full font-bold">Shortage Warning</span>
                                        </td>
                                    </tr>
                                </template>
                                <tr x-show="defaultersList.length === 0">
                                    <td colspan="6" class="p-6 text-center text-emerald-400 font-bold">
                                        🎉 No defaulters! All students maintain >= 75% attendance.
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- MARK ATTENDANCE TAB -->
            <div x-show="currentTab === 'mark'">
                <div class="grid grid-cols-4 gap-4 mb-6">
                    <select x-model="selectedMonth" @change="loadData()" class="w-full p-2.5 rounded-xl"><template x-for="m in months"><option :value="m" :selected="m == selectedMonth" x-text="m"></option></template></select>
                    <select x-model="selectedYear" @change="loadData()" class="w-full p-2.5 rounded-xl"><template x-for="y in years"><option :value="y" :selected="y == selectedYear" x-text="y"></option></template></select>
                    <select x-model="selectedSubject" @change="loadData()" class="w-full p-2.5 rounded-xl"><template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template></select>
                    <input type="date" x-model="selectedDate" @change="syncFromDate(); loadData()" class="w-full p-2.5 rounded-xl">
                </div>

                <div class="grid grid-cols-2 gap-8 items-start" x-show="students.length > 0">

                    <!-- ID CARD UI - ENLARGED -->
                    <div class="bg-gradient-to-b from-[#fefdfa] to-[#f8f5e9] text-slate-900 py-10 px-6 rounded-3xl shadow-2xl border-4 border-slate-300 max-w-sm mx-auto w-full min-h-[460px] flex flex-col justify-between relative">
                        <div>
                            <div class="bg-gradient-to-r from-emerald-700 to-emerald-900 text-white p-3 rounded-xl flex items-center gap-3 border-b-4 border-amber-400 mb-6">
                                <img :src="collegeLogo" class="w-10 h-10 bg-white rounded-full p-0.5 object-contain">
                                <div class="text-xs font-black leading-tight" x-text="collegeName.toUpperCase()"></div>
                            </div>
                            <div class="flex justify-center my-6">
                                <img :src="currentStudentPhoto" class="w-32 h-32 rounded-full object-cover border-4 border-sky-500 shadow-md bg-white">
                            </div>
                            <h2 class="text-center text-2xl font-black text-slate-900 mb-1" x-text="currentStudent.name"></h2>
                            <p class="text-center font-bold text-slate-700 text-xs mb-1" x-text="'ROLL NO : ' + currentStudent.roll_no"></p>
                            <p class="text-center font-bold text-slate-600 text-xs mb-4" x-text="'REG NO : ' + currentStudent.reg_no"></p>
                            <div class="bg-emerald-700 text-white text-center font-black py-1.5 rounded-lg text-xs mb-4" x-text="courseName + ' - ' + sectionName"></div>
                        </div>
                        <div class="text-[11px] space-y-2 font-semibold text-slate-800 border-t border-sky-400 border-dashed pt-4 mt-2">
                            <p><b>Email:</b> <span x-text="currentStudentDetails.email"></span></p>
                            <p><b>Contact:</b> <span x-text="currentStudentDetails.contact"></span></p>
                            <p><b>Guardian:</b> <span x-text="currentStudentDetails.parent_name + ' (' + currentStudentDetails.parent_contact + ')'"></span></p>
                        </div>
                    </div>

                    <div class="space-y-6">
                        <div class="grid grid-cols-2 gap-4">
                            <button @click="markStatusBtn('Present')" class="bg-emerald-500 hover:bg-emerald-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition">🟢 MARK PRESENT (P)</button>
                            <button @click="markStatusBtn('Absent')" class="bg-red-500 hover:bg-red-600 text-white font-black py-5 rounded-2xl shadow-xl text-lg transition">🔴 MARK ABSENT (A)</button>
                        </div>
                        <div>
                            <label class="block text-white font-bold text-sm mb-1">🔍 Search Student by Reg No:</label>
                            <input type="text" x-model="searchReg" @input="searchByReg" placeholder="Type Registration Number..." class="w-full p-3 rounded-xl shadow">
                        </div>
                        <div>
                            <label class="block text-white font-bold text-sm mb-1">🔍 Quick Jump</label>
                            <select x-model="currentIndex" @change="fetchStudentDetails" class="w-full p-3 rounded-xl">
                                <template x-for="(st, idx) in students"><option :value="idx" x-text="st.reg_no + ' - ' + st.name"></option></template>
                            </select>
                        </div>
                        <div class="flex gap-4">
                            <button @click="prevStudent" class="flex-1 bg-red-600 hover:bg-red-700 py-3 rounded-xl font-black shadow">◀ PREVIOUS</button>
                            <button @click="nextStudent" class="flex-1 bg-red-600 hover:bg-red-700 py-3 rounded-xl font-black shadow">NEXT ▶</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ATTENDANCE TABLE TAB -->
            <div x-show="currentTab === 'table'">
                <div class="grid grid-cols-3 gap-4 mb-4">
                    <select x-model="tableMonth" @change="loadTableData()" class="w-full p-2.5 rounded-xl"><template x-for="m in months"><option :value="m" :selected="m == tableMonth" x-text="m"></option></template></select>
                    <select x-model="tableYear" @change="loadTableData()" class="w-full p-2.5 rounded-xl"><template x-for="y in years"><option :value="y" :selected="y == tableYear" x-text="y"></option></template></select>
                    <select x-model="tableSubject" @change="loadTableData()" class="w-full p-2.5 rounded-xl"><template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template></select>
                </div>
                <div class="flex gap-4 mb-4">
                    <a :href="'/api/download_table_excel/' + userId + '?month=' + tableMonth + '&year=' + tableYear + '&subject=' + tableSubject" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl text-center shadow">📊 DOWNLOAD THIS TABLE TO EXCEL (.XLSX)</a>
                </div>
                <div class="mb-4">
                    <input type="text" x-model="tableSearchQuery" placeholder="🔍 Search student in table..." class="w-full p-3 rounded-xl shadow">
                </div>
                <div class="bg-sky-100 rounded-xl overflow-x-auto border-2 border-sky-400 shadow-2xl">
                    <table class="w-full text-slate-900 font-bold text-sm text-center math-grid-table border-collapse">
                        <thead>
                            <tr class="bg-blue-900 text-white">
                                <th class="p-3 border sticky col-reg bg-blue-900 z-20">Reg No</th>
                                <th class="p-3 border sticky col-roll bg-blue-900 z-20">Roll</th>
                                <th class="p-3 border sticky col-name bg-blue-900 z-20">Student Name</th>
                                <template x-for="d in tableNumDays">
                                    <th class="border text-xs day-cell bg-blue-900 text-white" x-text="d"></th>
                                </template>
                                <th class="p-3 border w-16 bg-blue-900 text-white">%</th>
                            </tr>
                        </thead>
                        <tbody>
                            <template x-for="st in filteredTableRows" :key="st.id">
                                <tr class="bg-sky-50 hover:bg-sky-200">
                                    <td class="p-2 border sticky col-reg bg-sky-50 z-10 font-mono text-xs" x-text="st.reg_no"></td>
                                    <td class="p-2 border sticky col-roll bg-sky-50 z-10 font-mono text-xs" x-text="st.roll_no"></td>
                                    <td class="p-2 border sticky col-name bg-sky-50 z-10 truncate font-semibold" x-text="st.name"></td>
                                    <template x-for="d in tableNumDays" :key="d">
                                        <td class="border day-cell text-center cursor-pointer select-none" 
                                            :class="st.days[d] === 'P' ? 'bg-emerald-500 text-white font-black' : (st.days[d] === 'A' ? 'bg-red-500 text-white font-black' : 'hover:bg-sky-200')" 
                                            x-text="st.days[d]"
                                            @click="toggleCellAttendance(st, d)"></td>
                                    </template>
                                    <td class="p-2 border font-black text-blue-800" x-text="st.pct + '%'"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- ABSENTEES TAB -->
            <div x-show="currentTab === 'absentees'">
                <div class="glass-card p-6 rounded-3xl shadow-2xl border-2 border-rose-500/50">
                    <div class="flex justify-between items-center mb-6">
                        <div>
                            <h2 class="text-2xl font-black text-rose-400">🔴 Daily Absentees Report</h2>
                            <p class="text-xs text-slate-300">View and share the list of absent students for a specific date.</p>
                        </div>
                        <div class="flex gap-2">
                            <a :href="'/api/download_absentees_pdf/' + userId + '?subject=' + absenteesSubject + '&date_str=' + absenteesDate" class="bg-rose-600 hover:bg-rose-700 text-white font-black text-xs py-2 px-3 rounded-xl shadow transition">📥 Export PDF</a>
                            <button @click="shareAbsenteesPdf()" class="bg-blue-600 hover:bg-blue-700 text-white font-black text-xs py-2 px-3 rounded-xl shadow transition">🔗 Share PDF</button>
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-4 mb-6">
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Subject</label>
                            <select x-model="absenteesSubject" @change="loadAbsentees()" class="w-full p-3 rounded-xl text-slate-900 font-bold">
                                <template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Date</label>
                            <input type="date" x-model="absenteesDate" @change="loadAbsentees()" class="w-full p-3 rounded-xl text-slate-900 font-bold">
                        </div>
                    </div>
                    
                    <div class="overflow-x-auto bg-slate-900/80 rounded-2xl border border-rose-500/30">
                        <table class="w-full text-sm text-center">
                            <thead>
                                <tr class="bg-rose-950/80 text-rose-300 font-bold border-b border-rose-500/40">
                                    <th class="p-3 w-16">Sl No</th>
                                    <th class="p-3 w-28">Roll No</th>
                                    <th class="p-3 w-40">Reg No</th>
                                    <th class="p-3 text-left">Student Name</th>
                                </tr>
                            </thead>
                            <tbody class="text-slate-200">
                                <template x-for="(st, index) in absenteesList">
                                    <tr class="border-b border-slate-800 hover:bg-rose-950/20 font-semibold">
                                        <td class="p-3 text-slate-400 font-mono" x-text="index + 1"></td>
                                        <td class="p-3 font-mono" x-text="st.roll_no"></td>
                                        <td class="p-3 text-sky-400" x-text="st.reg_no"></td>
                                        <td class="p-3 text-left font-bold text-white" x-text="st.name"></td>
                                    </tr>
                                </template>
                                <tr x-show="absenteesList.length === 0">
                                    <td colspan="4" class="p-6 text-center text-emerald-400 font-bold">
                                        🎉 No absentees found for this date and subject!
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- MONTHLY REPORT TAB -->
            <div x-show="currentTab === 'report'">
                <div class="grid grid-cols-2 gap-4 mb-4">
                    <select x-model="reportMonth" @change="loadReportData()" class="w-full p-2.5 rounded-xl"><template x-for="m in months"><option :value="m" :selected="m == reportMonth" x-text="m"></option></template></select>
                    <select x-model="reportYear" @change="loadReportData()" class="w-full p-2.5 rounded-xl"><template x-for="y in years"><option :value="y" :selected="y == reportYear" x-text="y"></option></template></select>
                </div>
                <div class="flex gap-4 mb-4">
                    <a :href="'/api/download_excel/' + userId + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-black py-3 rounded-xl text-center shadow">📊 DOWNLOAD EXCEL</a>
                    <a :href="'/api/download_pdf/' + userId + '?month=' + reportMonth + '&year=' + reportYear" class="flex-1 bg-red-600 hover:bg-red-700 text-white font-black py-3 rounded-xl text-center shadow">📥 DOWNLOAD PDF</a>
                    <button @click="shareViaEmail()" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-black py-3 rounded-xl text-center shadow">🔗 SHARE PDF</button>
                </div>
                <div class="mb-4">
                    <input type="text" x-model="reportSearchQuery" placeholder="🔍 Search student in report..." class="w-full p-3 rounded-xl shadow">
                </div>
                <div class="bg-sky-100 rounded-xl overflow-x-auto border-2 border-sky-400 shadow-2xl">
                    <table class="w-full text-slate-900 font-bold text-sm text-center">
                        <thead>
                            <tr class="bg-blue-900 text-white">
                                <th class="p-3 border">Roll No</th>
                                <th class="p-3 border">Reg No</th>
                                <th class="p-3 border text-left">Student Name</th>
                                <template x-for="sub in reportSubjects"><th class="p-2 border" x-text="sub"></th></template>
                                <th class="p-3 border">Overall %</th>
                            </tr>
                        </thead>
                        <tbody>
                            <template x-for="st in filteredReportRows">
                                <tr class="border-b bg-sky-50">
                                    <td class="p-3 border" x-text="st.roll_no"></td>
                                    <td class="p-3 border" x-text="st.reg_no"></td>
                                    <td class="p-3 border text-left" x-text="st.name"></td>
                                    <template x-for="sub in reportSubjects"><td class="p-2 border" x-text="st.subs[sub]"></td></template>
                                    <td class="p-3 border font-black text-emerald-700" x-text="st.overall"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- LEAVE INBOX TAB -->
            <div x-show="currentTab === 'leaves'">
                <div class="flex justify-between items-center mb-6">
                    <div>
                        <h2 class="text-2xl font-black text-white">✉️ Student Leave Applications Inbox</h2>
                        <p class="text-xs text-slate-300 mt-1">Review student applications, view documents and reply with approval/rejection remarks.</p>
                    </div>
                    <button @click="loadFacultyLeaves()" :disabled="isProcessing" class="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-bold px-4 py-2 rounded-xl text-xs shadow">🔄 Refresh</button>
                </div>
                
                <!-- SEARCH BAR FOR LEAVES -->
                <div class="mb-6">
                    <input type="text" x-model="leaveSearchQuery" placeholder="🔍 Search applications by Student Name or Reg No..." class="w-full p-3 rounded-xl shadow border-2 border-indigo-400/30 bg-slate-900 text-white font-bold text-sm focus:border-indigo-500">
                </div>

                <div class="space-y-4">
                    <template x-for="leave in filteredFacultyLeaves">
                        <div class="glass-card p-6 rounded-2xl border-l-8" :class="leave.status === 'Approved' ? 'border-emerald-500' : (leave.status === 'Rejected' ? 'border-red-500' : 'border-amber-500')">
                            <div class="flex justify-between items-start mb-2">
                                <div>
                                    <span class="bg-sky-950 text-sky-400 font-black px-2.5 py-1 rounded text-xs border border-sky-400/40" x-text="leave.leave_type"></span>
                                    <h3 class="text-lg font-black text-white mt-1" x-text="leave.student_name + ' (' + leave.reg_no + ')'"></h3>
                                    <p class="text-xs text-amber-300 font-bold" x-text="'Subject: ' + leave.subject + ' | Period: ' + leave.from_date + ' to ' + leave.to_date"></p>
                                </div>
                                <span class="px-3 py-1 rounded-full text-xs font-black shadow"
                                      :class="leave.status === 'Approved' ? 'bg-emerald-900 text-emerald-300 border border-emerald-500' : (leave.status === 'Rejected' ? 'bg-red-900 text-red-300 border border-red-500' : 'bg-amber-900 text-amber-300 border border-amber-500')"
                                      x-text="leave.status"></span>
                            </div>
                            <div class="bg-slate-900/90 p-3 rounded-xl border border-slate-700 text-slate-200 text-xs mb-3 whitespace-pre-wrap" x-text="leave.reason"></div>
                            
                            <!-- ATTACHMENT VIEW BUTTON -->
                            <div x-show="leave.document_data" class="mb-3">
                                <a :href="leave.document_data" download="Leave_Attachment" class="inline-flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-sky-400 text-[11px] font-bold py-1.5 px-3 rounded-lg border border-slate-600 transition shadow">
                                    📎 View / Download Attached Document
                                </a>
                            </div>

                            <div x-show="leave.faculty_remark" class="bg-indigo-950/60 p-2.5 rounded-xl border border-indigo-500/40 text-xs text-indigo-200 mb-3">
                                <b>Your Previous Remark:</b> <span x-text="leave.faculty_remark"></span>
                            </div>
                            <div class="pt-3 border-t border-slate-700 flex gap-3 items-center">
                                <input type="text" x-model="leaveRemarkInput[leave.id]" placeholder="Enter response / message for student..." class="flex-1 p-2.5 rounded-xl text-xs bg-white text-slate-900 font-bold">
                                <button @click="respondLeave(leave.id, 'Approved')" :disabled="isProcessing" class="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-black px-4 py-2.5 rounded-xl text-xs shadow">✅ Approve</button>
                                <button @click="respondLeave(leave.id, 'Rejected')" :disabled="isProcessing" class="bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white font-black px-4 py-2.5 rounded-xl text-xs shadow">❌ Reject</button>
                            </div>
                        </div>
                    </template>
                    <div x-show="filteredFacultyLeaves.length === 0" class="glass-card p-12 text-center rounded-2xl text-slate-400 font-bold">
                        📭 No matching leave applications found.
                    </div>
                </div>
            </div>

            <!-- RESET TAB -->
            <div x-show="currentTab === 'reset'">
                <div class="glass-card p-6 rounded-2xl space-y-6">
                    <h2 class="text-2xl font-black text-white">🧹 Reset / Clear Attendance Logs</h2>
                    <div class="grid grid-cols-3 gap-4">
                        <button @click="resetScope = 'single'" :class="resetScope === 'single' ? 'bg-emerald-600' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow">👤 Single Student</button>
                        <button @click="resetScope = 'class'" :class="resetScope === 'class' ? 'bg-red-600' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow">🏫 Class Reset</button>
                        <button @click="resetScope = 'date'" :class="resetScope === 'date' ? 'bg-red-700' : 'bg-slate-800'" class="p-4 rounded-xl font-bold shadow">📅 Date Reset</button>
                    </div>
                    <div class="grid grid-cols-3 gap-4">
                        <select x-show="resetScope === 'single'" x-model="resetReg" class="w-full p-3 rounded-xl"><option value="">--- Select Student ---</option><template x-for="st in students"><option :value="st.reg_no" x-text="st.reg_no + ' - ' + st.name"></option></template></select>
                        <select x-model="resetSubject" class="w-full p-3 rounded-xl"><option value="All Subjects">All Subjects</option><template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template></select>
                        <input type="date" x-show="resetScope === 'date'" x-model="resetDate" class="w-full p-3 rounded-xl">
                    </div>
                    <button @click="executeReset" :disabled="isProcessing" class="bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white font-black py-3 px-6 rounded-xl shadow">⚠️ Execute Purge / Reset Records</button>
                </div>
            </div>

            <!-- MANAGE STUDENTS TAB -->
            <div x-show="currentTab === 'students'" class="space-y-6">
                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-blue-400 mb-2">1️⃣ Register Students (Excel/CSV)</h3>
                        <input type="file" id="studentOnlyFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-blue-50 text-slate-900">
                        <button @click="importStudentsOnly" :disabled="isProcessing" class="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Add Students</button>
                        <div x-show="skippedImports && skippedImports.length > 0" class="mt-4 bg-red-900/40 border border-red-500/50 p-4 rounded-xl text-xs" style="display: none;">
                            <h4 class="text-red-400 font-bold mb-2">⚠️ Skipped (Already in another class):</h4>
                            <ul class="list-disc pl-4 text-slate-300 max-h-32 overflow-y-auto custom-scrollbar">
                                <template x-for="reg in skippedImports"><li x-text="reg"></li></template>
                            </ul>
                        </div>
                    </div>
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-emerald-400 mb-2">2️⃣ Bulk Mark Attendance (Excel/CSV)</h3>
                        <div class="grid grid-cols-2 gap-4 mb-4">
                            <select x-model="importSubject" class="w-full p-2.5 rounded-xl text-sm"><template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template></select>
                            <input type="date" x-model="importDate" class="w-full p-2.5 rounded-xl text-sm">
                        </div>
                        <input type="file" id="attendanceFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-emerald-50 text-slate-900">
                        <button @click="importAttendanceOnly" :disabled="isProcessing" class="w-full bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Mark Attendance from File</button>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">➕ Add Single Student</h3>
                        <form @submit.prevent="addStudent" class="space-y-4">
                            <input type="text" x-model="newStudent.reg_no" placeholder="Registration No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.roll_no" placeholder="Roll No" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="newStudent.name" placeholder="Full Name" required class="w-full p-3 rounded-xl">
                            <button type="submit" :disabled="isProcessing" class="w-full bg-blue-500 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Save Student</button>
                        </form>
                    </div>
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">📸 Upload Photo & Profile</h3>
                        <form @submit.prevent="saveStudentProfile" class="space-y-3">
                            <select x-model="profileReg" class="w-full p-2 rounded-xl"><option value="">--- Select Student ---</option><template x-for="st in students"><option :value="st.reg_no" x-text="st.reg_no + ' - ' + st.name"></option></template></select>
                            <input type="file" id="studentPhotoFile" class="w-full p-2 rounded-xl text-sm bg-sky-50">
                            <input type="email" x-model="profileForm.email" placeholder="Email" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.contact" placeholder="Contact" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.parent_name" placeholder="Guardian Name" class="w-full p-2 rounded-xl text-sm">
                            <input type="text" x-model="profileForm.parent_contact" placeholder="Guardian Contact" class="w-full p-2 rounded-xl text-sm">
                            <button type="submit" :disabled="isProcessing" class="w-full bg-blue-500 disabled:opacity-50 text-white font-black py-2.5 rounded-xl shadow">Save Profile</button>
                        </form>
                    </div>
                    <div class="glass-card p-6 rounded-2xl col-span-2 border-2 border-red-500/50">
                        <h3 class="text-xl font-black text-red-400 mb-2">⚠️ Delete All Students</h3>
                        <button @click="deleteAllStudents" :disabled="isProcessing" class="w-full bg-red-700 hover:bg-red-800 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Delete All Students & Data Forever</button>
                    </div>
                </div>
            </div>

            <!-- PROFILE TAB -->
            <div x-show="currentTab === 'profile'">
                <div class="grid grid-cols-2 gap-6">
                    <div class="glass-card p-6 rounded-2xl">
                        <h3 class="text-xl font-black text-sky-400 mb-4">🏫 Institutional Details</h3>
                        <form @submit.prevent="saveCollegeProfile" class="space-y-4">
                            <input type="text" x-model="collegeName" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="appSubtitle" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="courseName" required class="w-full p-3 rounded-xl">
                            <input type="text" x-model="sectionName" required class="w-full p-3 rounded-xl">
                            <button type="submit" :disabled="isProcessing" class="w-full bg-blue-500 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Save Info</button>
                        </form>
                    </div>
                    <div class="space-y-6">
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">📚 Add / Delete Subject</h3>
                            <form @submit.prevent="addSubject" class="space-y-3 mb-4">
                                <input type="text" x-model="newSubject" placeholder="Subject Name" required class="w-full p-3 rounded-xl">
                                <button type="submit" :disabled="isProcessing" class="w-full bg-blue-500 disabled:opacity-50 text-white font-black py-2.5 rounded-xl shadow">Add Subject</button>
                            </form>
                            <form @submit.prevent="deleteSubject" class="space-y-3">
                                <select x-model="delSubject" class="w-full p-3 rounded-xl"><option value="">--- Select Subject ---</option><template x-for="sub in subjects"><option :value="sub" x-text="sub"></option></template></select>
                                <button type="submit" :disabled="isProcessing" class="w-full bg-red-500 text-white font-black py-2.5 rounded-xl shadow">Delete Subject</button>
                            </form>
                        </div>
                        <div class="glass-card p-6 rounded-2xl">
                            <h3 class="text-xl font-black text-sky-400 mb-4">🖼️ Logo Upload</h3>
                            <input type="file" id="logoFile" class="w-full p-3 rounded-xl mb-4 text-sm bg-sky-50">
                            <button @click="uploadLogo" :disabled="isProcessing" class="w-full bg-blue-500 disabled:opacity-50 text-white font-black py-3 rounded-xl shadow">Upload Logo</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- STUDENT DASHBOARD -->
    <div x-show="loggedIn && userRole === 'student'" class="min-h-screen relative z-10 p-4 md:p-8" style="display: none;">
        <div class="max-w-5xl mx-auto glass-card p-6 rounded-3xl shadow-2xl mb-8 flex justify-between items-center border-t-4 border-emerald-500">
            <div class="flex items-center gap-4">
                <img :src="studentDashData?.logo || 'https://i.ibb.co/3s68K1v/tree-logo.png'" class="w-16 h-16 bg-white rounded-xl p-1 shadow object-contain">
                <div>
                    <h1 class="text-2xl font-black text-white" x-text="studentDashData?.college_name"></h1>
                    <p class="text-emerald-400 font-bold text-sm" x-text="studentDashData?.course"></p>
                </div>
            </div>
            <div class="flex gap-3">
                <button @click="openLeaveModal = true" class="bg-amber-500 hover:bg-amber-600 text-slate-950 font-black px-5 py-2.5 rounded-xl shadow-lg transition text-sm">✉️ Apply for Leave</button>
                <button @click="logout" class="bg-red-500 hover:bg-red-600 text-white font-black px-6 py-2.5 rounded-xl shadow-lg transition text-sm">🚪 Logout</button>
            </div>
        </div>

        <div class="max-w-5xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-8" x-show="studentDashData">
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
                                 x-text="studentDashData.overall_pct + '%'"></div>
                        </div>
                        <p class="mt-4 text-xs font-bold" :class="studentDashData.overall_pct >= 75 ? 'text-emerald-400' : 'text-red-400'">
                            <span x-text="studentDashData.overall_pct >= 75 ? '✅ Safe Attendance Zone' : '⚠️ Shortage Zone (< 75%)'"></span>
                        </p>
                    </div>
                </div>
            </div>

            <div class="col-span-1 md:col-span-2 space-y-8">
                <!-- 1. SUBJECT WISE COMPILATION -->
                <div class="glass-card p-6 rounded-3xl">
                    <h3 class="text-xl font-black text-amber-400 mb-6">📊 Subject-wise Compilation</h3>
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

                <!-- 2. DATE-WISE ATTENDANCE LOG -->
                <div class="glass-card p-6 rounded-3xl">
                    <h3 class="text-xl font-black text-emerald-400 mb-4 flex items-center gap-2">📅 Date-wise Attendance Register (P/A Status)</h3>
                    <div class="max-h-80 overflow-y-auto pr-2 custom-scrollbar">
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
                                        <td class="p-3 font-mono" x-text="rec.date"></td>
                                        <td class="p-3 font-bold" x-text="rec.subject"></td>
                                        <td class="p-3 text-center">
                                            <span class="px-3 py-1 rounded-full text-xs font-black shadow"
                                                  :class="rec.status === 'Present' ? 'bg-emerald-900 text-emerald-400 border border-emerald-500' : 'bg-red-900 text-red-400 border border-red-500'"
                                                  x-text="rec.status === 'Present' ? 'P' : 'A'"></span>
                                        </td>
                                    </tr>
                                </template>
                                <tr x-show="!studentDashData.history || studentDashData.history.length === 0">
                                    <td colspan="3" class="text-center p-6 text-slate-500">No attendance marked yet.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- 3. MY SUBMITTED LEAVE APPLICATIONS -->
                <div class="glass-card p-6 rounded-3xl">
                    <h3 class="text-xl font-black text-sky-400 mb-4">✉️ My Submitted Leave Applications</h3>
                    <div class="space-y-3 max-h-64 overflow-y-auto pr-2 custom-scrollbar">
                        <template x-for="l in (studentDashData.leaves || [])">
                            <div class="bg-slate-900/90 p-4 rounded-2xl border border-slate-700 text-xs">
                                <div class="flex justify-between items-start mb-2">
                                    <div>
                                        <span class="bg-sky-950 text-sky-400 font-black px-2 py-0.5 rounded text-[10px] border border-sky-400/40" x-text="l.leave_type"></span>
                                        <span class="font-bold text-white ml-2" x-text="l.subject"></span>
                                    </div>
                                    <span class="px-2.5 py-0.5 rounded-full font-black text-[11px]"
                                          :class="l.status === 'Approved' ? 'bg-emerald-900 text-emerald-400 border border-emerald-500' : (l.status === 'Rejected' ? 'bg-red-900 text-red-400 border border-red-500' : 'bg-amber-900 text-amber-400 border border-amber-500')"
                                          x-text="l.status"></span>
                                </div>
                                <p class="text-slate-400 mb-1"><b>Duration:</b> <span class="text-slate-200" x-text="l.from_date + ' to ' + l.to_date"></span></p>
                                <p class="text-slate-300 italic mb-2" x-text="'\"' + l.reason + '\"'"></p>
                                
                                <div x-show="l.document_data" class="mt-2 mb-3">
                                    <a :href="l.document_data" download="Leave_Document" class="text-[10px] text-sky-400 hover:text-sky-300 underline font-bold">📎 View Attached Document</a>
                                </div>

                                <div x-show="l.faculty_remark" class="bg-slate-950 p-2.5 rounded-xl border border-sky-500/30 text-sky-300">
                                    <b>Faculty Remark:</b> <span x-text="l.faculty_remark"></span>
                                </div>
                            </div>
                        </template>
                        <p x-show="!studentDashData.leaves || studentDashData.leaves.length === 0" class="text-slate-500 text-center py-4">No leave applications submitted yet.</p>
                    </div>
                </div>
            </div>
        </div>

        <!-- LEAVE APPLICATION MODAL -->
        <div x-show="openLeaveModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4" style="display: none;">
            <div class="bg-slate-900 border-2 border-sky-400 rounded-3xl w-full max-w-2xl overflow-hidden shadow-2xl">
                <div class="bg-slate-950 px-6 py-4 border-b border-slate-800 flex justify-between items-center">
                    <span class="text-sky-400 font-black text-sm">✉️ Compose Leave Application</span>
                    <button @click="openLeaveModal = false" class="text-slate-400 hover:text-white font-bold text-xl">&times;</button>
                </div>
                <form @submit.prevent="submitLeaveApplication" class="p-6 space-y-4">
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Leave Category</label>
                            <select x-model="leaveForm.leave_type" class="w-full p-2.5 rounded-xl text-xs" required>
                                <option>🏥 Sick / Medical Leave</option>
                                <option>🎉 College Event Leave</option>
                                <option>✈️ Personal Leave</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">Subject / Context</label>
                            <input type="text" x-model="leaveForm.subject" placeholder="e.g. All Subjects" class="w-full p-2.5 rounded-xl text-xs" required>
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">From Date</label>
                            <input type="date" x-model="leaveForm.from_date" class="w-full p-2.5 rounded-xl text-xs" required>
                        </div>
                        <div>
                            <label class="block text-sky-400 font-bold text-xs mb-1">To Date</label>
                            <input type="date" x-model="leaveForm.to_date" class="w-full p-2.5 rounded-xl text-xs" required>
                        </div>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Reason / Statement</label>
                        <textarea x-model="leaveForm.reason" rows="4" placeholder="Respected Sir/Madam, I am applying for leave..." required class="w-full p-3 rounded-xl text-xs font-normal"></textarea>
                    </div>
                    <div>
                        <label class="block text-sky-400 font-bold text-xs mb-1">Attach Medical/Event Document (Optional)</label>
                        <input type="file" id="leaveAttachmentFile" class="w-full p-2 rounded-xl text-xs bg-slate-800 text-white border border-sky-400/50">
                    </div>
                    <div class="flex justify-end gap-3 pt-2">
                        <button type="button" @click="openLeaveModal = false" class="px-5 py-2.5 rounded-xl text-slate-400 text-xs font-bold">Cancel</button>
                        <button type="submit" :disabled="isProcessing" class="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white font-black px-6 py-2.5 rounded-xl shadow text-xs flex items-center gap-2">
                            <span x-show="isProcessing">⏳ Submitting...</span>
                            <span x-show="!isProcessing">🚀 Send to Faculty</span>
                        </button>
                    </div>
                </form>
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
                userRole: '',
                authRole: 'faculty',
                isLogin: true,
                isProcessing: false,
                authForm: { username: '', password: '' },
                studentForm: { reg_no: '', name: '' },
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
                defaultersList: [],

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
                
                // ABSENTEES LIST
                absenteesList: [],
                absenteesSubject: '',
                absenteesDate: curDate,

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

                openLeaveModal: false,
                leaveForm: { leave_type: '🏥 Sick / Medical Leave', subject: 'All Subjects', from_date: curDate, to_date: curDate, reason: '' },
                facultyLeaves: [],
                leaveRemarkInput: {},
                leaveSearchQuery: '',

                init() { this.syncFromDate(); },
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
                get pendingLeavesCount() {
                    return this.facultyLeaves.filter(l => l.status === 'Pending').length;
                },
                get filteredFacultyLeaves() {
                    if (this.leaveSearchQuery.trim() === '') return this.facultyLeaves;
                    let q = this.leaveSearchQuery.toLowerCase();
                    return this.facultyLeaves.filter(l => 
                        (l.student_name && l.student_name.toLowerCase().includes(q)) || 
                        (l.reg_no && l.reg_no.toLowerCase().includes(q))
                    );
                },
                async submitAuth() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
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
                            if (!this.isLogin) alert("Account created successfully!");
                            await this.loadData();
                            await this.loadFacultyLeaves();
                        } else {
                            this.authError = data.detail || "Authentication Failed.";
                        }
                    } catch(e) {
                        this.authError = "Server Connection Error.";
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async submitStudentAuth() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('reg_no', this.studentForm.reg_no);
                    formData.append('name', this.studentForm.name);
                    try {
                        let res = await fetch('/api/student_login', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            this.userRole = 'student';
                            this.userId = data.reg_no; 
                            this.loggedIn = true;
                            this.authError = '';
                            await this.loadStudentDashboard(data.faculty_id, data.reg_no);
                        } else {
                            this.authError = data.detail || "Student Login Failed.";
                        }
                    } catch(e) {
                        this.authError = "Server Connection Error.";
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async loadStudentDashboard(fac_id, reg_no) {
                    try {
                        let res = await fetch(`/api/student_dashboard_data/${fac_id}/${reg_no}`);
                        let data = await res.json();
                        if (data.error) {
                            alert(data.error);
                            this.logout();
                        } else {
                            this.studentDashData = data;
                        }
                    } catch (e) {
                        alert("Error loading dashboard data.");
                    }
                },
                async submitLeaveApplication() {
                    if (!this.studentDashData || this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('faculty_id', this.studentDashData.faculty_id);
                    formData.append('reg_no', this.studentDashData.student.reg_no);
                    formData.append('student_name', this.studentDashData.student.name);
                    formData.append('leave_type', this.leaveForm.leave_type);
                    formData.append('subject', this.leaveForm.subject);
                    formData.append('from_date', this.leaveForm.from_date);
                    formData.append('to_date', this.leaveForm.to_date);
                    formData.append('reason', this.leaveForm.reason);

                    let fileInput = document.getElementById('leaveAttachmentFile');
                    if (fileInput && fileInput.files.length > 0) {
                        formData.append('file', fileInput.files[0]);
                    }

                    try {
                        let res = await fetch('/api/apply_leave', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.openLeaveModal = false;
                            this.leaveForm.reason = '';
                            if (fileInput) fileInput.value = '';
                            await this.loadStudentDashboard(this.studentDashData.faculty_id, this.studentDashData.student.reg_no);
                        } else {
                            alert("Failed: " + data.detail);
                        }
                    } catch(e) {
                        alert("Error connecting to server.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async loadFacultyLeaves() {
                    try {
                        let res = await fetch(`/api/leaves/${this.userId}`);
                        let data = await res.json();
                        this.facultyLeaves = data.leaves || [];
                    } catch(e) {
                        console.error(e);
                    }
                },
                async respondLeave(leaveId, status) {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let remark = this.leaveRemarkInput[leaveId] || "";
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('leave_id', leaveId);
                    formData.append('status', status);
                    formData.append('faculty_remark', remark);

                    try {
                        let res = await fetch('/api/update_leave_status', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.loadFacultyLeaves();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async loadData() {
                    try {
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
                        this.defaultersList = data.defaulters || [];
                        this.subjects = data.subjects;
                        if (!this.selectedSubject && this.subjects.length > 0) this.selectedSubject = this.subjects[0];
                        if (!this.tableSubject && this.subjects.length > 0) this.tableSubject = this.subjects[0];
                        if (!this.importSubject && this.subjects.length > 0) this.importSubject = this.subjects[0];
                        if (!this.absenteesSubject && this.subjects.length > 0) this.absenteesSubject = this.subjects[0];
                        this.students = data.students;
                        if (this.students.length > 0) this.fetchStudentDetails();
                    } catch(e) {
                        console.error(e);
                    }
                },
                async loadTableData() {
                    if (!this.tableSubject && this.subjects.length > 0) this.tableSubject = this.subjects[0];
                    let res = await fetch(`/api/attendance_table/${this.userId}?month=${this.tableMonth}&year=${this.tableYear}&subject=${this.tableSubject}`);
                    let data = await res.json();
                    this.tableNumDays = data.num_days;
                    this.tableRows = data.table_data;
                    this.tableTotalClasses = data.total_classes;
                },
                async loadAbsentees() {
                    if (!this.absenteesSubject && this.subjects.length > 0) this.absenteesSubject = this.subjects[0];
                    if (!this.absenteesDate) this.absenteesDate = this.selectedDate;
                    if (!this.absenteesSubject) return;
                    try {
                        let res = await fetch(`/api/absentees/${this.userId}?subject=${this.absenteesSubject}&date_str=${this.absenteesDate}`);
                        let data = await res.json();
                        this.absenteesList = data.absentees || [];
                    } catch(e) {
                        console.error("Error loading absentees:", e);
                    }
                },
                async toggleCellAttendance(student, day) {
                    let current = student.days[day];
                    let nextStatus = current === 'P' ? 'Absent' : (current === 'A' ? 'Clear' : 'Present');
                    let displayVal = current === 'P' ? 'A' : (current === 'A' ? '' : 'P');
                    student.days[day] = displayVal;

                    let distinctDays = new Set();
                    for (let s of this.tableRows) {
                        for (let d = 1; d <= this.tableNumDays; d++) {
                            if (s.days[d] === 'P' || s.days[d] === 'A') distinctDays.add(d);
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
                        if (!res.ok) this.loadTableData();
                    } catch(e) {
                        this.loadTableData();
                    }
                },
                async loadReportData() {
                    let res = await fetch(`/api/compile_report/${this.userId}?month=${this.reportMonth}&year=${this.reportYear}`);
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
                    return this.tableRows.filter(st => (st.name && st.name.toLowerCase().includes(q)) || (st.reg_no && st.reg_no.toLowerCase().includes(q)) || (st.roll_no && String(st.roll_no).toLowerCase().includes(q)));
                },
                get filteredReportRows() {
                    if (this.reportSearchQuery.trim() === '') return this.reportRows;
                    let q = this.reportSearchQuery.toLowerCase();
                    return this.reportRows.filter(st => (st.name && st.name.toLowerCase().includes(q)) || (st.reg_no && st.reg_no.toLowerCase().includes(q)) || (st.roll_no && String(st.roll_no).toLowerCase().includes(q)));
                },
                async fetchStudentDetails() {
                    let reg = this.currentStudent.reg_no;
                    if (!reg) return;
                    let res = await fetch(`/api/student_details/${this.userId}/${reg}`);
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

                    try {
                        let res = await fetch('/api/mark_attendance', { method: 'POST', body: formData });
                        if (res.ok) {
                            if (this.currentIndex < this.students.length - 1) {
                                this.currentIndex++;
                                this.fetchStudentDetails();
                            }
                            await this.loadData();
                        } else {
                            let data = await res.json();
                            alert("Error saving attendance: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
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
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.newStudent.reg_no);
                    formData.append('roll_no', this.newStudent.roll_no);
                    formData.append('name', this.newStudent.name);
                    try {
                        let res = await fetch('/api/add_student', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.newStudent = { reg_no: '', roll_no: '', name: '' };
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async deleteStudent() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.delRegNo);
                    try {
                        let res = await fetch('/api/delete_student', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.delRegNo = '';
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async deleteAllStudents() {
                    if (!confirm("Are you sure? This cannot be undone.")) return;
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    try {
                        let res = await fetch('/api/delete_all_students', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async importStudentsOnly() {
                    let fileInput = document.getElementById('studentOnlyFile');
                    if (fileInput.files.length === 0) { alert('Select a file.'); return; }
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    try {
                        let res = await fetch('/api/import_students', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.skippedImports = data.skipped || [];
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async importAttendanceOnly() {
                    let fileInput = document.getElementById('attendanceFile');
                    if (fileInput.files.length === 0) { alert('Select a file.'); return; }
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    formData.append('subject', this.importSubject);
                    formData.append('date_str', this.importDate);
                    try {
                        let res = await fetch('/api/import_attendance', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async executeReset() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('scope', this.resetScope);
                    formData.append('subject', this.resetSubject);
                    if (this.resetScope === 'single') formData.append('reg_no', this.resetReg);
                    if (this.resetScope === 'date') formData.append('date_str', this.resetDate);
                    try {
                        let res = await fetch('/api/reset_attendance', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async addSubject() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.newSubject);
                    try {
                        let res = await fetch('/api/add_subject', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.newSubject = '';
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async deleteSubject() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('subject_name', this.delSubject);
                    try {
                        let res = await fetch('/api/delete_subject', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            this.delSubject = '';
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async uploadLogo() {
                    let fileInput = document.getElementById('logoFile');
                    if (fileInput.files.length === 0) { alert('Select a file.'); return; }
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('file', fileInput.files[0]);
                    try {
                        let res = await fetch('/api/upload_logo', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            this.collegeLogo = data.logo_url;
                            alert(data.message);
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async saveCollegeProfile() {
                    if (this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('college_name', this.collegeName);
                    formData.append('subtitle', this.appSubtitle);
                    formData.append('course_name', this.courseName);
                    formData.append('section_name', this.sectionName);
                    try {
                        let res = await fetch('/api/save_college_profile', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.loadData();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                async saveStudentProfile() {
                    if (!this.profileReg || this.isProcessing) return;
                    this.isProcessing = true;
                    let formData = new FormData();
                    formData.append('user_id', this.userId);
                    formData.append('reg_no', this.profileReg);
                    formData.append('email', this.profileForm.email);
                    formData.append('contact', this.profileForm.contact);
                    formData.append('parent_name', this.profileForm.parent_name);
                    formData.append('parent_contact', this.profileForm.parent_contact);
                    formData.append('res_type', this.profileForm.res_type);
                    let photoInput = document.getElementById('studentPhotoFile');
                    if (photoInput.files.length > 0) formData.append('file', photoInput.files[0]);
                    try {
                        let res = await fetch('/api/save_student_profile', { method: 'POST', body: formData });
                        let data = await res.json();
                        if (res.ok) {
                            alert(data.message);
                            await this.fetchStudentDetails();
                        } else {
                            alert("Error: " + data.detail);
                        }
                    } catch(e) {
                        alert("Network error.");
                    } finally {
                        this.isProcessing = false;
                    }
                },
                
                async shareFileNative(pdfUrl, fileName, titleText) {
                    try {
                        if (navigator.share && navigator.canShare) {
                            let response = await fetch(pdfUrl);
                            let blob = await response.blob();
                            let file = new File([blob], fileName, { type: 'application/pdf' });
                            
                            if (navigator.canShare({ files: [file] })) {
                                await navigator.share({
                                    title: titleText,
                                    text: 'Please find the attached document.',
                                    files: [file]
                                });
                                return;
                            }
                        }
                        alert("Native sharing is not supported on this browser. Downloading instead...");
                        let a = document.createElement('a');
                        a.href = pdfUrl;
                        a.download = fileName;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                    } catch (e) {
                        console.log("Sharing cancelled or failed.", e);
                    }
                },

                async shareViaEmail() {
                    let pdfUrl = `/api/download_pdf/${this.userId}?month=${this.reportMonth}&year=${this.reportYear}`;
                    let fileName = `Attendance_Report_${this.reportMonth}_${this.reportYear}.pdf`;
                    await this.shareFileNative(pdfUrl, fileName, "Monthly Attendance Report");
                },
                async shareDefaultersPdf() {
                    let pdfUrl = `/api/download_defaulters_pdf/${this.userId}?month=${this.selectedMonth}&year=${this.selectedYear}&subject=${this.selectedSubject}`;
                    let fileName = `Defaulters_${this.selectedSubject}_${this.selectedMonth}_${this.selectedYear}.pdf`;
                    await this.shareFileNative(pdfUrl, fileName, "Defaulters Report");
                },
                async shareAbsenteesPdf() {
                    let pdfUrl = `/api/download_absentees_pdf/${this.userId}?subject=${this.absenteesSubject}&date_str=${this.absenteesDate}`;
                    let fileName = `Absentees_${this.absenteesSubject}_${this.absenteesDate}.pdf`;
                    await this.shareFileNative(pdfUrl, fileName, "Daily Absentees Report");
                },
                
                logout() {
                    this.loggedIn = false;
                    this.userRole = '';
                    this.userId = '';
                    this.studentDashData = null;
                    this.facultyLeaves = [];
                    this.skippedImports = [];
                    this.leaveSearchQuery = '';
                }
            }
        }
    </script>
</body>
</html>
"""