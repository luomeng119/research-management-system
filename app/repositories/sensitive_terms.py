"""CAS-controlled vocabulary head and append-only history."""
import sqlalchemy as sa


class SensitiveTermSetsRepository:
    def __init__(self,engine):
        self.engine=engine
        metadata=sa.MetaData()
        self.head=sa.Table('sensitive_term_sets',metadata,autoload_with=engine)
        self.versions=sa.Table('sensitive_term_set_versions',metadata,autoload_with=engine)
        self.users=sa.Table('users',metadata,autoload_with=engine)

    def actor(self,connection,actor_id):
        statement=sa.select(self.users.c.id,self.users.c.role,self.users.c.status).where(self.users.c.id==actor_id)
        if connection.dialect.name=='postgresql':
            statement=statement.with_for_update()
        return connection.execute(statement).mappings().first()

    def current_number(self,connection):
        return connection.scalar(sa.select(self.head.c.current_version).where(self.head.c.id==1))

    def advance(self,connection,expected):
        return connection.execute(self.head.update().where(self.head.c.id==1,self.head.c.current_version==expected).values(current_version=expected+1)).rowcount==1

    def insert(self,connection,values):
        connection.execute(self.versions.insert().values(**values))

    def _query(self,*,include_rules=True):
        columns=[c for c in self.versions.c if include_rules or c.name!='rules']
        author=sa.func.coalesce(sa.func.nullif(self.users.c.name,''),self.users.c.username).label('created_by_name')
        return sa.select(*columns,author).select_from(self.versions.join(self.users,self.versions.c.created_by==self.users.c.id))

    def get_version(self,connection,version_no):
        return connection.execute(self._query().where(self.versions.c.version_no==version_no)).mappings().first()

    def history(self,connection,page,page_size):
        total=connection.scalar(sa.select(sa.func.count()).select_from(self.versions))
        rows=connection.execute(self._query(include_rules=False).order_by(self.versions.c.version_no.desc()).offset((page-1)*page_size).limit(page_size)).mappings().all()
        return rows,total
