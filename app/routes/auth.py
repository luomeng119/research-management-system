# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from app.models import UserModel

bp = Blueprint('auth', __name__, url_prefix='/auth')

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        user_model = UserModel()
        user = user_model.get_by_username(username)
        
        if user and user['status'] == 'pending':
            flash('账号正在审核中，请等待管理员批准', 'warning')
            return render_template('login.html')
        
        verify_result = user_model.verify(username, password)
        if verify_result:
            session['user'] = username
            session['name'] = user['name']  # 存name字段，页面显示友好名称
            session['role'] = user['role']
            return redirect(url_for('index'))
        else:
            flash('用户名或密码错误', 'error')
    
    return render_template('login.html')

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        name = request.form.get('name', '').strip()
        
        if not username or not password or not name:
            flash('请填写完整信息', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('两次密码输入不一致', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('密码至少6个字符', 'error')
            return render_template('register.html')
        
        user_model = UserModel()
        
        if user_model.get_by_username(username):
            flash('用户名已存在', 'error')
            return render_template('register.html')
        
        # 新用户默认为待审核状态
        user_model.add(username, password, '用户', name, status='pending')
        flash('注册成功，请等待管理员审核', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('register.html')

@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
