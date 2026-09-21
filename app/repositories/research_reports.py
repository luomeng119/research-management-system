"""Report persistence. Revision content has no update or delete operation."""
from __future__ import annotations

import uuid
import sqlalchemy as sa


class ResearchReportsRepository:
    def __init__(self, engine):
        self.engine = engine
        metadata = sa.MetaData()
        self.reports = sa.Table('research_reports', metadata, autoload_with=engine)
        self.versions = sa.Table('research_report_versions', metadata, autoload_with=engine)
        self.parents = {
            'PROPOSAL': sa.Table('proposals', metadata, autoload_with=engine),
            'PROJECT': sa.Table('project_registry', metadata, autoload_with=engine),
        }
        self.users = sa.Table('users', metadata, autoload_with=engine)
        self.drafts = (sa.Table('research_report_drafts', metadata, autoload_with=engine)
                       if sa.inspect(engine).has_table('research_report_drafts') else None)

    @staticmethod
    def identifier(connection, value):
        parsed = uuid.UUID(str(value))
        return parsed if connection.dialect.name == 'postgresql' else str(parsed)

    def parent(self, connection, object_type, object_id):
        table = self.parents[object_type]
        return connection.execute(sa.select(table.c.id, table.c.business_id).where(
            table.c.business_id == object_id
        )).mappings().all()

    def get(self, connection, report_id):
        r = self.reports
        p = self.parents['PROPOSAL']
        j = self.parents['PROJECT']
        return connection.execute(sa.select(r, sa.func.coalesce(p.c.business_id, j.c.business_id).label('object_id')).select_from(
            r.outerjoin(p, r.c.proposal_id == p.c.id).outerjoin(j, r.c.project_registry_id == j.c.id)
        ).where(r.c.id == self.identifier(connection, report_id))).mappings().first()

    def insert_report(self, connection, values):
        payload = dict(values)
        for key in ['id', 'proposal_id', 'project_registry_id']:
            if payload.get(key) is not None:
                payload[key] = self.identifier(connection, payload[key])
        connection.execute(self.reports.insert().values(**payload))

    def append_version(self, connection, values):
        payload = dict(values)
        if 'redaction_snapshot' in payload and payload['redaction_snapshot'] is None:
            payload['redaction_snapshot'] = sa.null()
        for key in ['id', 'report_id', 'draft_id']:
            if payload.get(key) is not None:
                payload[key] = self.identifier(connection, payload[key])
        connection.execute(self.versions.insert().values(**payload))

    def advance(self, connection, report_id, expected, actor, now):
        return connection.execute(self.reports.update().where(
            self.reports.c.id == self.identifier(connection, report_id),
            self.reports.c.current_version == expected,
        ).values(current_version=expected + 1, updated_by=actor, updated_at=now)).rowcount == 1

    def list_for_parent(self, connection, object_type, parent_id, page, page_size):
        column = self.reports.c.proposal_id if object_type == 'PROPOSAL' else self.reports.c.project_registry_id
        where = column == self.identifier(connection, parent_id)
        total = connection.scalar(sa.select(sa.func.count()).select_from(self.reports).where(where))
        r, p, j = self.reports, self.parents['PROPOSAL'], self.parents['PROJECT']
        rows = connection.execute(sa.select(r, sa.func.coalesce(p.c.business_id, j.c.business_id).label('object_id')).select_from(
            r.outerjoin(p, r.c.proposal_id == p.c.id).outerjoin(j, r.c.project_registry_id == j.c.id)
        ).where(where).order_by(r.c.updated_at.desc(), r.c.id.desc()).offset((page - 1) * page_size).limit(page_size)).mappings().all()
        return rows, total

    def _author_name(self):
        return sa.func.coalesce(sa.func.nullif(self.users.c.name, ''), self.users.c.username).label('created_by_name')

    def get_version(self, connection, report_id, version_no):
        return connection.execute(sa.select(self.versions, self._author_name()).select_from(self.versions.join(self.users, self.versions.c.created_by == self.users.c.id)).where(
            self.versions.c.report_id == self.identifier(connection, report_id),
            self.versions.c.version_no == version_no,
        )).mappings().first()

    def history(self, connection, report_id, page, page_size):
        where = self.versions.c.report_id == self.identifier(connection, report_id)
        total = connection.scalar(sa.select(sa.func.count()).select_from(self.versions).where(where))
        # List metadata only; a full revision is fetched explicitly.
        columns = [c for c in self.versions.c if c.name != 'body']
        rows = connection.execute(sa.select(*columns, self._author_name()).select_from(self.versions.join(self.users, self.versions.c.created_by == self.users.c.id)).where(where).order_by(
            self.versions.c.version_no.desc()
        ).offset((page - 1) * page_size).limit(page_size)).mappings().all()
        return rows, total

    def actor_exists(self, connection, actor):
        return connection.scalar(sa.select(self.users.c.id).where(self.users.c.id == actor)) is not None

    def get_draft(self, connection, report_id, draft_id):
        if self.drafts is None:
            return None
        return connection.execute(sa.select(self.drafts).where(
            self.drafts.c.report_id == self.identifier(connection, report_id),
            self.drafts.c.id == self.identifier(connection, draft_id),
        )).mappings().first()

    def lock_current(self, connection, report_id):
        statement = sa.select(self.reports.c.current_version).where(
            self.reports.c.id == self.identifier(connection, report_id))
        if connection.dialect.name == 'postgresql':
            statement = statement.with_for_update()
        return connection.scalar(statement)
