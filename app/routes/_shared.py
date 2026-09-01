# -*- coding: utf-8 -*-
"""Shared utilities for routes."""
import os
import json
from datetime import datetime

# Default FOLDER_TYPES for research projects
RESEARCH_FOLDER_TYPES = ['任务输入文件', '研究成果文件', '院内审查文件', '机关审查文件', '成果上报文件']


def build_folder_tree(project_dir, base_path='', level=0, folder_types=None):
    """Build a folder tree structure for a project directory.

    Args:
        project_dir: The root project directory
        base_path: Relative path from project_dir (empty for root)
        level: Current recursion depth
        folder_types: List of fixed folder names to create at root (default: RESEARCH_FOLDER_TYPES)

    Returns:
        List of folder node dictionaries
    """
    if folder_types is None:
        folder_types = RESEARCH_FOLDER_TYPES

    result = []
    if not base_path:
        # First add the fixed folders defined by folder_types
        for folder in folder_types:
            folder_path = os.path.join(project_dir, folder)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path, exist_ok=True)
            files = []
            if os.path.exists(folder_path):
                for f in os.listdir(folder_path):
                    # Skip hidden directories (like .history)
                    if f.startswith('.'):
                        continue
                    f_path = os.path.join(folder_path, f)
                    if os.path.isfile(f_path):
                        # Check for version history
                        history_dir = os.path.join(folder_path, '.history')
                        has_history = 'false'
                        current_version = 'v1'
                        if os.path.exists(history_dir):
                            name, ext = os.path.splitext(f)
                            version_file = os.path.join(history_dir, f"{name}_versions.json")
                            if os.path.exists(version_file):
                                with open(version_file, 'r', encoding='utf-8') as vf:
                                    version_data = json.load(vf)
                                    if f in version_data and len(version_data[f]) > 0:
                                        has_history = 'true'
                                        current_version = f"v{version_data[f][-1]['version'] + 1}"

                        files.append({
                            'name': f,
                            'size': os.path.getsize(f_path),
                            'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'),
                            'path': os.path.join(folder, f).replace('\\', '/'),
                            'has_history': has_history,
                            'current_version': current_version
                        })
            node = {'name': folder, 'path': folder, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
            if os.path.exists(folder_path):
                subdirs = [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))]
                for subdir in subdirs:
                    node['children'].extend(build_folder_tree(project_dir, os.path.join(folder, subdir), level + 1, folder_types))
            result.append(node)

        # Then add user-created folders in the root directory
        if os.path.exists(project_dir):
            for item in os.listdir(project_dir):
                item_path = os.path.join(project_dir, item)
                # Only add folders not in folder_types
                if os.path.isdir(item_path) and item not in folder_types:
                    files = []
                    for f in os.listdir(item_path):
                        f_path = os.path.join(item_path, f)
                        if os.path.isfile(f_path):
                            files.append({'name': f, 'size': os.path.getsize(f_path), 'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'), 'path': os.path.join(item, f).replace('\\', '/')})
                    node = {'name': item, 'path': item, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
                    # Recursively add subdirectories
                    subdirs = [d for d in os.listdir(item_path) if os.path.isdir(os.path.join(item_path, d))]
                    for subdir in subdirs:
                        node['children'].extend(build_folder_tree(project_dir, os.path.join(item, subdir), level + 1, folder_types))
                    result.append(node)
    else:
        folder_path = os.path.join(project_dir, base_path)
        if not os.path.exists(folder_path):
            return []
        folder_name = os.path.basename(base_path)
        files = []
        for f in os.listdir(folder_path):
            f_path = os.path.join(folder_path, f)
            if os.path.isfile(f_path):
                files.append({'name': f, 'size': os.path.getsize(f_path), 'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'), 'path': os.path.join(base_path, f).replace('\\', '/')})
        node = {'name': folder_name, 'path': base_path, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
        subdirs = [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))]
        for subdir in subdirs:
            node['children'].extend(build_folder_tree(project_dir, os.path.join(base_path, subdir), level + 1, folder_types))
        result.append(node)
    return result
