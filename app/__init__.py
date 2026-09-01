# -*- coding: utf-8 -*-
from flask import Flask
import os
from config import SECRET_KEY, DEBUG, DATA_DIR, UPLOAD_DIR, DOCUMENTS_DIR, VERSION

def create_app():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__,
               template_folder=os.path.join(base_dir, 'templates'),
               static_folder=os.path.join(base_dir, 'static'))
    app.secret_key = SECRET_KEY
    app.config['DEBUG'] = DEBUG
    app.config['DATA_DIR'] = DATA_DIR
    app.config['UPLOAD_DIR'] = UPLOAD_DIR
    app.config['DOCUMENTS_DIR'] = DOCUMENTS_DIR
    app.config['VERSION'] = VERSION

    # [REQ-013-fix] 使用 Flask-Session filesystem session 替代 cookie session
    # 原因：1200+ 条数据时 session['equipment_import_preview']['processed'] 太大，
    # 超过 cookie 4KB 限制，导致 session 被截断 → 预览页找不到数据 → 302 redirect 回首页
    from flask_session import Session
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SESSION_FILE_DIR'] = os.path.join(DATA_DIR, 'flask_sessions')
    app.config['SESSION_PERMANENT'] = False
    app.config['SESSION_USE_SIGNER'] = True
    os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)
    Session(app)

    # 初始化数据库表
    from app.models import EquipmentModel, ExpertModel, ExpertGroupModel, StandardModel, EquipmentGroupModel, UserModel, LLMModel, InferenceServerStatus, HostDeviceCategoryModel, ResearchUnitModel, KnowledgeSubclassModel
    ExpertGroupModel().create_tables()
    EquipmentGroupModel().create_tables()
    LLMModel().create_table()
    InferenceServerStatus().create_table()
    HostDeviceCategoryModel().init_defaults()
    ResearchUnitModel().init_defaults()
    KnowledgeSubclassModel().init_defaults()

    # 启动时创建默认管理员账户
    user_model = UserModel()
    if not user_model.get_by_username('admin'):
        user_model.add('admin', 'admin123', '管理员', '系统管理员')
        print('[初始化] 默认管理员账户已创建: admin / admin123')
    
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
    
    # test_bp 定义
    from flask import Blueprint, render_template
    test_bp = Blueprint('test', __name__, url_prefix='/test')
    
    @test_bp.route('/document_preview')
    def document_preview():
        return render_template('test/document_preview.html')

    @test_bp.route('/tree')
    def test_tree():
        return render_template('test_tree.html')
    
    # 方案论证模块
    from app.routes.argumentation import init_argumentation_routes
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
    app.register_blueprint(test_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(utils.bp)
    app.register_blueprint(expense.bp)
    app.register_blueprint(documents.bp)
    app.register_blueprint(host_devices.bp)
    app.register_blueprint(research_units.bp)
    app.register_blueprint(generic_tables_bp)
    app.register_blueprint(generic_tables_api_bp)
    
    # 启动时预加载 RapidOCR 模型（避免第一次 OCR 时等待模型加载）
    print('[启动] 预加载 OCR 模型...')
    from app.ocr.recognizer import _get_rapid_ocr
    _get_rapid_ocr()
    print('[启动] OCR 模型预加载完成')

    # 初始化方案论证模块
    init_argumentation_routes(app)
    app.register_blueprint(template_bp)
    
    # 上传文件的静态路由
    @app.route('/uploads/<path:filename>')
    def uploaded_file(filename):
        from flask import send_from_directory
        # app/__init__.py 的父目录/uploads = 项目根/uploads，但文件实际存在 app/uploads/
        # 改为直接用 app/uploads/ 目录
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
        return send_from_directory(upload_dir, filename)
    
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

