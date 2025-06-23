from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from datetime import datetime, timedelta
import calendar,json
import os
import pandas as pd
from utils.lunar_handler import LunarConverter
from utils.excel_manager import LogManager
from auth.auth_manager import AuthManager
from flask_login import login_required

app = Flask(__name__)
app.secret_key = os.urandom(24)
#app.config['SESSION_PERMANENT'] = False  # [1,6](@ref)
app.config['DATA_PATH'] = 'data'
app.config['PERMANENT_SESSION_LIFETIME'] = 600  # #默认登录时长，10分钟[1](@ref)
# 加载凭据文件
with open('username&key.txt',encoding="utf-8") as f:
    f=json.load(f)
    key = f['key']
    name = f['name']
# 初始化认证模块
auth = AuthManager(default_user=name, key=key)

@app.before_request
def check_auth():
    if request.endpoint not in ['login', 'static']:
        if 'locked_until' in session and datetime.now() < session['locked_until']:
            return "账户已锁定，请1小时后再试", 403
        if not session.get('authenticated'):
            return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        result = auth.validate_user(username, password)
        
        if result == "success":
            session['authenticated'] = True
            session.pop('failed_attempts', None)
            return redirect(url_for('index'))
            
        session['failed_attempts'] = session.get('failed_attempts', 0) + 1
        if session['failed_attempts'] >= 3:
            session['locked_until'] = (datetime.now() + timedelta(hours=1)).timestamp()
        return result, 401
    return render_template('login.html')

@app.route('/')
#@login_required
def index():
    return render_template('index.html', 
                         default_date=datetime.now().strftime("%Y-%m-%d"))

@app.route('/get_calendar')
def get_calendar_data():
    year = int(request.args.get('year'))
    month = int(request.args.get('month'))
    
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdays2calendar(year, month)
    
    lunar_data = []
    for week in weeks:
        new_week = []
        for day, _ in week:
            if day == 0:
                new_week.append({'solar': '', 'lunar': '', 'date': ''})
            else:
                lunar = LunarConverter.solar_to_lunar(year, month, day)
                new_week.append({
                    'solar': day,
                    'lunar': lunar['cn_day'],
                    'date': f"{year}-{month:02d}-{day:02d}",
                    'is_today': LunarConverter.is_today(year, month, day)
                })
        lunar_data.append(new_week)
    
    return jsonify({
        'weeks': lunar_data,
        'month_title': f"{year}年{month}月"
    })

@app.route('/get_log', methods=['GET'])
def get_log():
    date_str = request.args.get('date')
    return jsonify(LogManager.load_log(date_str))

@app.route('/save_log', methods=['POST'])
def save_log():
    data = request.json
    LogManager.save_log(data)
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    os.makedirs(app.config['DATA_PATH'], exist_ok=True)
    app.run(host='0.0.0.0',port=80)
