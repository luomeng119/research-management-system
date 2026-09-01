from flask import Blueprint, render_template, request, redirect, url_for, session
from app.models import EquipmentModel, OperationLogModel
import os

bp = Blueprint('security_logs', __name__, url_prefix='/security_logs')

@bp.route('/logs/security')
def logs():
    if 'user' not in session:
        return redirect('/auth/login')
    
    operator = request.args.get('operator')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    page = int(request.args.get('page', 1))
    per_page = 50
    
    log_model = OperationLogModel()
    all_logs = log_model.search(module='security', operator=operator, file_name=file_name, start_date=start_date, end_date=end_date, limit=1000)
    
    total = len(all_logs)
    start = (page - 1) * per_page
    end = start + per_page
    logs = all_logs[start:end]
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    
    equipment_model = EquipmentModel()
    equipment = equipment_model.get_all()
    
    category_groups = {}
    for e in equipment:
        category = e['category'] if e.get('category') else '未分类'
        if category not in category_groups:
            category_groups[category] = []
        category_groups[category].append(e)
    
    return render_template('equipment/logs.html', logs=logs, equipment=equipment, category_groups=category_groups, module_name='security', module_title='安全保密项目', page=page, total_pages=total_pages, total=total)