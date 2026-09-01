# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file, current_app
from app.models import OperationLogModel
import os
from datetime import datetime

bp = Blueprint('templates', __name__, url_prefix='/templates')


def _safe_filename(filename):
    filename = os.path.basename(filename.replace('\\', '/'))
    filename = re.sub(r'[^\w\s.-]', '_', filename).strip('. ')
    return filename or 'unnamed'

# 模板根目录（项目根目录下的templates）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

def build_template_tree(base_path='', level=0):
    """构建模板目录树"""
    result = []
    
    # 一级默认分类
    default_cats = ['财务模板', '会务模板', '公文模板', '方案模板', '其他模板']
    
    if not base_path:
        # 遍历默认分类
        for cat in default_cats:
            cat_path = os.path.join(TEMPLATE_DIR, cat)
            if not os.path.exists(cat_path):
                os.makedirs(cat_path, exist_ok=True)
            # 使用递归函数获取完整目录树
            node = build_single_folder(cat, cat, level)
            result.append(node)
        
        # 添加用户创建的目录（一级）
        if os.path.exists(TEMPLATE_DIR):
            for item in os.listdir(TEMPLATE_DIR):
                item_path = os.path.join(TEMPLATE_DIR, item)
                if os.path.isdir(item_path) and item not in default_cats and not item.startswith('.'):
                    node = build_single_folder(item, item, level)
                    result.append(node)
    else:
        # 子目录
        result.append(build_single_folder(os.path.basename(base_path), base_path, level))
    
    return result

def build_single_folder(name, path, level):
    """构建单个文件夹节点 - 完全递归获取"""
    full_path = os.path.join(TEMPLATE_DIR, path) if path else os.path.join(TEMPLATE_DIR, name)
    
    files = []
    children = []
    
    if not os.path.exists(full_path):
        return {'name': name, 'path': path, 'level': level, 'fileCount': 0, 'files': [], 'children': []}
    
    # 获取当前目录的文件和子目录
    try:
        items = os.listdir(full_path)
    except:
        return {'name': name, 'path': path, 'level': level, 'fileCount': 0, 'files': [], 'children': []}
    
    for item in items:
        if item.startswith('.'):
            continue
        item_path = os.path.join(full_path, item)
        rel_path = os.path.join(path, item).replace('\\', '/') if path else item
        
        if os.path.isfile(item_path):
            files.append({
                'name': item,
                'size': os.path.getsize(item_path),
                'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M'),
                'path': rel_path
            })
        elif os.path.isdir(item_path):
            # 递归获取子目录
            children.append(build_single_folder(item, rel_path, level + 1))
    
    return {
        'name': name,
        'path': path,
        'level': level,
        'fileCount': len(files),
        'files': files,
        'children': children
    }

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取当前分类参数
    current_cat = request.args.get('cat', '')
    
    # 构建目录树
    if current_cat:
        # 只显示指定分类
        folder_tree = [build_single_folder(current_cat, current_cat, 0)]
    else:
        # 显示全部
        folder_tree = build_template_tree()
    
    return render_template('templates/index.html', folder_tree=folder_tree, current_cat=current_cat)

@bp.route('/debug_tree')
def debug_tree():
    """调试用：返回完整目录树（无需登录）"""
    import json
    tree = build_template_tree()
    return json.dumps({'tree': tree}, ensure_ascii=False, indent=2)

@bp.route('/test_tree')
def test_tree():
    """测试页面：显示目录树原始数据"""
    import json
    tree = build_template_tree()
    tree_json = json.dumps(tree, ensure_ascii=False, indent=2)
    html = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>目录树测试</title>
    <link href="/static/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-4">
<h1>目录树测试页面</h1>
<p class="text-muted">这是测试页面，直接显示后端返回的原始数据</p>
<hr>
<h3>原始JSON数据：</h3>
<pre class="bg-light p-3" style="max-height:500px;overflow:auto">''' + tree_json + '''</pre>
<hr>
<h3>数据验证：</h3>
<script>
var data = ''' + tree_json + ''';
document.write('<p>一级分类数量: ' + data.length + '</p>');
data.forEach(function(item) {
    document.write('<p>' + item.name + ': ' + (item.children ? item.children.length : 0) + ' 个子目录</p>');
});
</script>
</body>
</html>
    '''
    return html

@bp.route('/upload', methods=['POST'])
def upload():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    folder = request.form.get('folder', '')
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})
    
    # 上传到指定目录，默认为"其他模板"
    target_dir = folder if folder else '其他模板'
    folder_path = os.path.join(TEMPLATE_DIR, target_dir)
    os.makedirs(folder_path, exist_ok=True)
    
    # 自动在文件名中添加日期，格式：（某年某月某日版）
    original_filename = _safe_filename(file.filename)
    if '.' in original_filename:
        name_part = original_filename.rsplit('.', 1)[0]
        ext_part = original_filename.rsplit('.', 1)[1]
        date_suffix = datetime.now().strftime('（%Y年%m月%d日版）')
        filename = f"{name_part}{date_suffix}.{ext_part}"
    else:
        date_suffix = datetime.now().strftime('（%Y年%m月%d日版）')
        filename = f"{original_filename}{date_suffix}"
    
    file_path = os.path.join(folder_path, filename)
    
    # 重复上传自动覆盖
    file.save(file_path)
    
    # 记录操作日志
    log_model = OperationLogModel()
    log_model.add(module='templates', operation_type='上传文件', file_name=filename, operator=session.get('user', '未知'), detail='分类: ' + target_dir)
    
    return jsonify({'success': True, 'message': '上传成功'})

@bp.route('/upload_folder', methods=['POST'])
def upload_folder():
    """文件夹上传 - 保持原目录结构"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    target_folder = request.form.get('folder', '')  # 目标分类
    
    if 'files' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件夹'})
    
    files = request.files.getlist('files')
    if not files or len(files) == 0:
        return jsonify({'success': False, 'message': '文件夹为空'})
    
    uploaded_count = 0
    base_target = os.path.join(TEMPLATE_DIR, target_folder) if target_folder else TEMPLATE_DIR
    
    for file in files:
        if file.filename:
            # webkitdirectory 会传递完整路径，如 "子文件夹/文件名.docx"
            relative_path = file.filename.replace('\\', '/')
            
            # 目标路径
            target_path = os.path.join(base_target, relative_path)
            target_dir = os.path.dirname(target_path)
            
            # 创建目标目录
            os.makedirs(target_dir, exist_ok=True)
            
            # 自动在文件名中添加日期
            dir_part = os.path.dirname(relative_path)
            name_part = os.path.basename(relative_path)
            if '.' in name_part:
                fname, ext = name_part.rsplit('.', 1)
                date_suffix = datetime.now().strftime('（%Y年%m月%d日版）')
                new_filename = f"{fname}{date_suffix}.{ext}"
            else:
                date_suffix = datetime.now().strftime('（%Y年%m月%d日版）')
                new_filename = f"{name_part}{date_suffix}"
            
            if dir_part and dir_part != '.':
                final_path = os.path.join(target_dir, new_filename)
            else:
                final_path = os.path.join(base_target, new_filename)
            
            # 保存文件
            file.save(final_path)
            uploaded_count += 1
            
            # 记录日志
            log_model = OperationLogModel()
            log_model.add(module='templates', operation_type='上传文件夹', file_name=relative_path, 
                       operator=session.get('user', '未知'), detail='目标: ' + target_folder)
    
    return jsonify({'success': True, 'message': f'上传成功 {uploaded_count} 个文件'})

@bp.route('/create_folder', methods=['POST'])
def create_folder():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    folder_name = request.form.get('folder_name', '').strip()
    parent_folder = request.form.get('parent_folder', '').strip()
    
    if not folder_name:
        return jsonify({'success': False, 'message': '请输入文件夹名称'})
    
    # 创建目录
    if parent_folder:
        new_path = os.path.join(TEMPLATE_DIR, parent_folder, folder_name)
    else:
        new_path = os.path.join(TEMPLATE_DIR, folder_name)
    
    if os.path.exists(new_path):
        return jsonify({'success': False, 'message': '文件夹已存在'})
    
    os.makedirs(new_path, exist_ok=True)
    return jsonify({'success': True, 'message': '创建成功'})

@bp.route('/download/<path:filepath>')
def download(filepath):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    full_path = os.path.normpath(os.path.join(TEMPLATE_DIR, filepath))
    if not full_path.startswith(os.path.normpath(TEMPLATE_DIR)):
        return "非法路径", 400
    if not os.path.exists(full_path):
        return "文件不存在", 404
    filename = os.path.basename(filepath)
    return send_file(full_path, as_attachment=True, download_name=filename)

@bp.route('/delete_file', methods=['POST'])
def delete_file():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    filepath = request.form.get('filepath', '').strip()
    if not filepath:
        return jsonify({'success': False, 'message': '文件路径不能为空'})
    
    full_path = os.path.join(TEMPLATE_DIR, filepath)
    if os.path.exists(full_path):
        filename = os.path.basename(filepath)
        os.remove(full_path)
        # 记录操作日志
        log_model = OperationLogModel()
        log_model.add(module='templates', operation_type='删除文件', file_name=filename, operator=session.get('user', '未知'))
        return jsonify({'success': True, 'message': '删除成功'})
    else:
        return jsonify({'success': False, 'message': '文件不存在'})

@bp.route('/delete_folder', methods=['POST'])
def delete_folder():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    folder_path = request.form.get('folder', '').strip()
    if not folder_path:
        return jsonify({'success': False, 'message': '文件夹路径不能为空'})
    
    full_path = os.path.join(TEMPLATE_DIR, folder_path)
    if os.path.exists(full_path) and os.path.isdir(full_path):
        import shutil
        shutil.rmtree(full_path)
        # 记录操作日志
        log_model = OperationLogModel()
        log_model.add(module='templates', operation_type='删除文件夹', file_name=folder_path, operator=session.get('user', '未知'))
        return jsonify({'success': True, 'message': '删除成功'})
    else:
        return jsonify({'success': False, 'message': '文件夹不存在'})

@bp.route('/rename_file', methods=['POST'])
def rename_file():
    """文件名重命名"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    filepath = request.form.get('filepath', '').strip()
    new_name = request.form.get('new_name', '').strip()
    
    if not filepath or not new_name:
        return jsonify({'success': False, 'message': '文件路径和新文件名不能为空'})
    
    full_path = os.path.join(TEMPLATE_DIR, filepath)
    if not os.path.exists(full_path):
        return jsonify({'success': False, 'message': '文件不存在'})
    
    dir_path = os.path.dirname(filepath)
    new_filepath = os.path.join(dir_path, new_name) if dir_path else new_name
    new_full_path = os.path.join(TEMPLATE_DIR, new_filepath)
    
    if os.path.exists(new_full_path):
        return jsonify({'success': False, 'message': '文件名已存在'})
    
    try:
        os.rename(full_path, new_full_path)
        # 记录操作日志
        log_model = OperationLogModel()
        log_model.add(module='templates', operation_type='重命名文件', file_name=f"{filepath} -> {new_name}", operator=session.get('user', '未知'))
        return jsonify({'success': True, 'message': '重命名成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@bp.route('/logs/<module_name>')
def logs(module_name):
    """操作日志页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取筛选参数
    operator = request.args.get('operator')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    log_model = OperationLogModel()
    logs = log_model.search(
        module=module_name,
        operator=operator,
        file_name=file_name,
        start_date=start_date,
        end_date=end_date,
        limit=100
    )
    
    module_names = {
        'equipment': '设备知识库',
        'standards': '标准法规库',
        'templates': '科研模板'
    }
    
    return render_template('templates/logs.html', 
                          logs=logs, 
                          module_name=module_name,
                          module_title=module_names.get(module_name, '日志'),
                          show_templates_link=True)
