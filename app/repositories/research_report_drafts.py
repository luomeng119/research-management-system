"""Append-only generated draft storage; models never write report versions."""
import sqlalchemy as sa
from app.repositories.research_reports import ResearchReportsRepository


class ResearchReportDraftsRepository:
    def __init__(self,engine):
        self.engine=engine
        self.table=sa.Table('research_report_drafts',sa.MetaData(),autoload_with=engine)

    def get(self,connection,report_id,draft_id):
        return connection.execute(sa.select(self.table).where(
            self.table.c.id==ResearchReportsRepository.identifier(connection,draft_id),
            self.table.c.report_id==ResearchReportsRepository.identifier(connection,report_id),
        )).mappings().first()

    def insert(self,connection,values):
        payload=dict(values)
        if 'redaction_snapshot' in payload and payload['redaction_snapshot'] is None:
            payload['redaction_snapshot'] = sa.null()
        for key in ['id','report_id']:
            payload[key]=ResearchReportsRepository.identifier(connection,payload[key])
        connection.execute(self.table.insert().values(**payload))
