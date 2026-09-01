# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from app.models import UserModel, DIRECTORIES
from functools import wraps

bp = Blueprint('users', __name__, url_prefix='/users')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        if session.get('role') != '管理员':
            flash('需要管理员权限', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

@bp.route('/')
@admin_required
def index():
    user_model = UserModel()
    users = user_model.get_all()
    # 获取每个用户的目录权限
    users_with_perms = []
    for user in users:
        perms = user_model.get_directory_permissions(user['username'])
        user['directory_permissions'] = perms
        users_with_perms.append(user)
    return render_template('users/index.html', users=users_with_perms, directories=DIRECTORIES)

@bp.route('/approve/<username>', methods=['POST'])
@admin_required
def approve(username):
    user_model = UserModel()
    user = user_model.get_by_username(username)
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'})
    
    user_model.update(username, status='active')
    return jsonify({'success': True, 'message': '审核通过'})

@bp.route('/reject/<username>', methods=['POST'])
@admin_required
def reject(username):
    user_model = UserModel()
    user = user_model.get_by_username(username)
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'})
    
    user_model.delete(username)
    return jsonify({'success': True, 'message': '已拒绝'})

@bp.route('/delete/<username>', methods=['POST'])
@admin_required
def delete(username):
    if username == session.get('user'):
        return jsonify({'success': False, 'message': '不能删除自己的账号'})
    
    user_model = UserModel()
    user_model.delete(username)
    return jsonify({'success': True, 'message': '删除成功'})

# ========== 目录权限管理 ==========
@bp.route('/permissions/<username>', methods=['GET', 'POST'])
@admin_required
def permissions(username):
    """设置用户目录权限"""
    user_model = UserModel()
    
    if request.method == 'POST':
        permissions = {}
        for directory in DIRECTORIES:
            permissions[directory] = request.form.get(directory, 'hidden')
        
        user_model.set_directory_permissions(username, permissions)
        flash(f'用户 {username} 的目录权限已更新', 'success')
        return redirect(url_for('users.index'))
    
    # GET: 显示权限设置页面
    user = user_model.get_by_username(username)
    if not user:
        flash('用户不存在', 'error')
        return redirect(url_for('users.index'))
    
    current_perms = user_model.get_directory_permissions(username)
    return render_template('users/permissions.html', username=username, permissions=current_perms, directories=DIRECTORIES)

# ========== 修改密码 ==========
@bp.route('/change-password', methods=['GET', 'POST'])
def change_password():
    """用户修改自己的密码"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        old_password = request.form.get('old_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not old_password or not new_password:
            flash('请填写所有字段', 'error')
            return redirect(url_for('users.change_password'))
        
        if new_password != confirm_password:
            flash('两次输入的密码不一致', 'error')
            return redirect(url_for('users.change_password'))
        
        user_model = UserModel()
        success, message = user_model.update_password(session['user'], old_password, new_password)
        
        if success:
            flash(message, 'success')
            return redirect(url_for('index'))
        else:
            flash(message, 'error')
            return redirect(url_for('users.change_password'))
    
    return render_template('users/change_password.html')
