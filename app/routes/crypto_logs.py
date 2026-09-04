from flask import Blueprint, current_app, render_template, request, redirect, session

from app.services.projects import ProjectServiceError

bp = Blueprint('crypto_logs', __name__, url_prefix='/crypto_logs')

@bp.route('/logs/crypto')
def logs():
    if 'user' not in session:
        return redirect('/auth/login')
    
    operator = request.args.get('operator')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    page = int(request.args.get('page', 1))
    per_page = 50
    
    service = current_app.extensions.get('project_service')
    if service is None:
        return '项目服务未就绪', 503
    try:
        result = service.list_logs(
            category='CRYPTO_APPLICATION', page=page,
            page_size=per_page, operator=operator, project_name=file_name,
            start_date=start_date, end_date=end_date,
        )
    except ProjectServiceError as error:
        return error.message, error.status_code
    logs = result['items']
    total = result['total']
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return render_template('equipment/logs.html', logs=logs, module_name='crypto', module_title='密码应用项目', page=page, total_pages=total_pages, total=total)
