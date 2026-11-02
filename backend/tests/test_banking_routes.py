"""Verify customer routes are included in the main application's OpenAPI contract."""
from __future__ import annotations

import unittest

from backend.main import app


class BankingRoutesTests(unittest.TestCase):
    def test_competition_workflow_routes_are_registered(self) -> None:
        paths = app.openapi()["paths"]
        for path, method in (
            ("/api/banking/session", "post"),
            ("/api/banking/readiness", "get"),
            ("/api/banking/capabilities", "get"),
            ("/api/banking/goal-plans/prepare", "post"),
            ("/api/banking/goal-plans/{identifier}", "get"),
            ("/api/banking/goal-plans/{identifier}", "patch"),
            ("/api/banking/goal-plans/{identifier}/compile", "post"),
            ("/api/banking/goal-plans/{identifier}/activate", "post"),
            ("/api/banking/execution/{kind}/{identifier}", "get"),
            ("/api/banking/execution-reconciliation", "get"),
            ("/api/banking/execution-device-acceptance", "get"),
            ("/api/banking/missions/{mid}", "get"),
            ("/api/banking/security/storage", "get"),
            ("/api/banking/evaluation/external-routing", "get"),
            ("/api/banking/operator/identity", "get"),
            ("/api/banking/operator/audit", "get"),
            ("/api/banking/auth/device-check/options", "post"),
            ("/api/banking/auth/device-check/verify", "post"),
            ("/api/banking/auth/device-check/status", "get"),
            ("/api/banking/research/enroll", "post"),
            ("/api/banking/research/report", "get"),
            ("/api/banking/research/withdraw", "post"),
            ("/api/banking/intent", "post"),
            ("/api/banking/transfers/prepare", "post"),
            ("/api/banking/bills", "get"),
            ("/api/banking/subscriptions", "get"),
            ("/api/banking/plans/prepare", "post"),
            ("/api/banking/wealth/profile", "post"),
            ("/api/banking/wealth/orders/prepare", "post"),
            ("/api/banking/wealth/redemptions/prepare", "post"),
            ("/api/banking/auth/register/options", "post"),
            ("/api/banking/auth/authorize/verify", "post"),
            ("/api/banking/missions/prepare", "post"),
            ("/api/banking/missions/{mid}/revise", "post"),
            ("/api/banking/missions/{mid}/evidence", "get"),
            ("/api/banking/missions/{mid}/changes", "get"),
            ("/api/banking/missions/{mid}/replan", "post"),
            ("/api/banking/operator/reviews/{rid}/evidence", "get"),
            ("/api/banking/external/entities/lookup", "post"),
            ("/api/banking/operator/reviews/{rid}/decision", "post"),
            ("/api/banking/conversation/messages", "post"),
            ("/api/banking/evidence/{recipient_id}", "get"),
            ("/api/banking/evidence/{recipient_id}/explain", "post"),
            ("/api/banking/evidence-fixtures", "post"),
            ("/api/banking/evaluation/report", "get"),
            ("/api/banking/external/notices", "get"),
            ("/api/banking/external/plaid/connect", "post"),
            ("/api/banking/external/plaid/sync", "post"),
            ("/api/banking/external/plaid/disconnect", "post"),
            ("/api/banking/external/benchmark", "get"),
            ("/api/banking/statements/preview", "post"),
            ("/api/banking/statements/{draft_id}/confirm", "post"),
            ("/api/banking/fx/rates", "get"),
            ("/api/banking/fx/quotes", "post"),
            ("/api/banking/fx/quotes/{identifier}/{action}", "post"),
            ("/api/banking/external/benchmark/recall", "get"),
            ("/api/banking/external/benchmark/robustness", "get"),
        ):
            with self.subTest(path=path):
                self.assertIn(method, paths[path])
