# -*- coding: utf-8 -*-
from flask import Flask, redirect, request, session, url_for
import os

PUBLIC_ENDPOINTS = frozenset({'auth.login', 'healthz', 'static'})


def create_app(test_config=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__,
               template_folder=os.path.join(base_dir, 'templates'),
               static_folder=os.path.join(base_dir, 'static'))
    app.config.from_object('config')
    if test_config:
        app.config.update(test_config)

    if not app.config.get('TESTING'):
        secret_key = os.environ.get('FLASK_SECRET_KEY')
        if not secret_key:
            raise RuntimeError(
                'FLASK_SECRET_KEY must be configured for non-testing environments'
            )
        app.config['SECRET_KEY'] = secret_key

    # [REQ-013-fix] 使用 Flask-Session filesystem session 替代 cookie session
    # 原因：1200+ 条数据时 session['equipment_import_preview']['processed'] 太大，
    # 超过 cookie 4KB 限制，导致 session 被截断 → 预览页找不到数据 → 302 redirect 回首页
    from flask_session import Session
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config.setdefault(
        'SESSION_FILE_DIR',
        os.path.join(app.config['DATA_DIR'], 'flask_sessions'),
    )
    app.config['SESSION_PERMANENT'] = False
    app.config['SESSION_USE_SIGNER'] = True
    os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)
    Session(app)
    
    # 直接导入所有蓝图
    from app.routes import api, auth, projects, equipment, standards, users, templates
    from app.routes import crypto_projects, security_projects, crypto_logs, security_logs
    from app.routes import experts, expert_groups, equipment_groups
    from app.routes import utils
    from app.routes import expense
    from app.routes import documents
    from app.routes import host_devices
    from app.routes import research_units
    from app.routes.generic_tables import bp as generic_tables_bp
    from app.routes.generic_tables import bp2 as generic_tables_api_bp
    from app.routes.preview import bp as preview_bp
    
    # 方案论证模块
    from app.routes.argumentation import argumentation_bp
    from app.routes.argumentation.template_routes import template_bp
    
    # 注册所有蓝图
    app.register_blueprint(api.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(equipment.bp)
    app.register_blueprint(standards.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(templates.bp)
    app.register_blueprint(security_projects.bp)
    app.register_blueprint(crypto_projects.bp)
    app.register_blueprint(crypto_logs.bp)
    app.register_blueprint(security_logs.bp)
    app.register_blueprint(experts.bp)
    app.register_blueprint(expert_groups.bp)
    app.register_blueprint(equipment_groups.bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(utils.bp)
    app.register_blueprint(expense.bp)
    app.register_blueprint(documents.bp)
    app.register_blueprint(host_devices.bp)
    app.register_blueprint(research_units.bp)
    app.register_blueprint(generic_tables_bp)
    app.register_blueprint(generic_tables_api_bp)
    
    app.register_blueprint(argumentation_bp, url_prefix='/argumentation')
    app.register_blueprint(template_bp)

    @app.route('/healthz')
    def healthz():
        return {'status': 'ok'}

    @app.before_request
    def require_authenticated_user():
        if request.endpoint not in PUBLIC_ENDPOINTS and 'user' not in session:
            return redirect(url_for('auth.login'))
    
    # 路由
    @app.route('/')
    def index():
        from flask import render_template, session, redirect, url_for
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        
        from app.models import UserModel, DIRECTORIES
        user_model = UserModel()
        role = session.get('role')
        
        if role == '管理员':
            visible_directories = DIRECTORIES
        else:
            perms = user_model.get_directory_permissions(session.get('user'))
            visible_directories = [d for d, v in perms.items() if v == 'visible']
        
        return render_template('index.html', visible_directories=visible_directories)
    
    return app
