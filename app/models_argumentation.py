"""Compatibility facade for retained argumentation; PostgreSQL is authoritative."""


def _repository():
    from flask import current_app
    from app.repositories.argumentation import ArgumentationRepository

    repository = current_app.extensions.get("argumentation_repository")
    if repository is None:
        engine = current_app.extensions.get("database_engine")
        if engine is None:
            raise RuntimeError("PostgreSQL is required for argumentation")
        repository = ArgumentationRepository(engine)
        current_app.extensions["argumentation_repository"] = repository
    return repository


def save_revision(project_id, category, content, change_note, expected_version):
    from flask import current_app, request, session

    repository = _repository()
    with repository.engine.begin() as connection:
        doc_id, version_num = repository.save_revision(
            connection,
            project_id=project_id,
            category=category,
            content=content,
            editor=session.get("name") or session["user"],
            change_note=change_note,
            expected_version=expected_version,
        )
        current_app.extensions["audit_service"].record(
            connection,
            event_name="project_record_added",
            user_id=session["user_id"],
            object_type="PROJECT",
            object_id=project_id,
            result="SUCCESS",
            request_id=getattr(request, "request_id", "unknown"),
            duration_ms=0,
            properties={"record_type": "ARGUMENTATION_DOCUMENT_VERSION"},
        )
    return doc_id, version_num


def init_argumentation_db():
    """Validate migrated tables; runtime initialization never creates or drops data."""
    _repository()


def get_template_by_category(category):
    return _repository().get_template_by_category(category)


def get_all_templates():
    return _repository().get_all_templates()


def save_template(template_id, name, category, file_path, chapter_tree):
    return _repository().save_template(
        template_id, name, category, file_path, chapter_tree
    )


def get_document_by_project(project_id, category):
    return _repository().get_document_by_project(project_id, category)


def get_document_by_id(doc_id):
    return _repository().get_document_by_id(doc_id)


def get_document_versions(doc_id):
    return _repository().get_document_versions(doc_id)


def get_version_by_num(doc_id, version_num):
    return next(
        (
            version
            for version in get_document_versions(doc_id)
            if version["version_num"] == version_num
        ),
        None,
    )


def get_projects(category, project_id=None):
    return _repository().get_projects(category, project_id)


def get_equipment():
    return _repository().get_equipment()
