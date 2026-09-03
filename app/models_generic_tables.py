# -*- coding: utf-8 -*-
"""Compatibility facade for the retained generic-table API.

Persistence and transaction ownership live in the injected PostgreSQL-backed
service. This module intentionally contains no database bootstrap or fallback.
"""
from __future__ import annotations

from flask import current_app, has_app_context

from app.services.generic_tables import GenericTablesError


class GenericTableModel:
    def __init__(self, service=None):
        if service is None and has_app_context():
            service = current_app.extensions.get("generic_tables_service")
        self._service = service

    def _required(self):
        if self._service is None:
            raise GenericTablesError("GENERIC_TABLES_UNAVAILABLE", "通用表格服务未就绪", 503)
        return self._service

    def get_all(self):
        # A deliberately partial application can still render its navigation.
        return self._service.get_all() if self._service is not None else []

    def __getattr__(self, name):
        return getattr(self._required(), name)
