from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.rules.models import TransactionRule, UpdateOrCreateTransactionRuleAction
from apps.rules.tasks import check_for_transaction_rules
from apps.transactions.models import Transaction


def run_check_for_transaction_rules_without_worker_wrapper(**kwargs):
    task_func = check_for_transaction_rules.func
    task_func = getattr(task_func, "__wrapped__", task_func)

    return task_func(**kwargs)


class CheckForTransactionRulesTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="rules@example.com",
            password="testpass123",
        )
        self.currency = Currency.objects.create(
            code="USD",
            name="US Dollar",
            decimal_places=2,
        )
        self.account = Account.objects.create(
            name="Main Account",
            currency=self.currency,
            owner=self.user,
        )

    @patch("apps.rules.signals.check_for_transaction_rules.defer")
    def test_update_or_create_action_can_clear_category_from_none_expression(
        self, mock_defer
    ):
        source_transaction = Transaction.objects.create(
            account=self.account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("10.00"),
            date=date(2026, 5, 4),
            reference_date=date(2026, 5, 1),
            description="Source without category",
            category=None,
            owner=self.user,
        )
        rule = TransactionRule.objects.create(
            active=True,
            on_create=False,
            on_update=True,
            name="Copy transaction",
            trigger="True",
            owner=self.user,
        )
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            set_account="account_id",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_amount="amount",
            set_description="'Generated transaction'",
            set_category="category_name",
        )

        run_check_for_transaction_rules_without_worker_wrapper(
            instance_id=source_transaction.id,
            user_id=self.user.id,
            signal="transaction_updated",
        )

        generated_transaction = Transaction.objects.get(
            description="Generated transaction"
        )
        self.assertIsNone(generated_transaction.category)

    @patch("apps.rules.signals.check_for_transaction_rules.defer")
    def test_update_or_create_action_set_mute_sets_mute_not_is_paid(self, mock_defer):
        source_transaction = Transaction.objects.create(
            account=self.account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("10.00"),
            date=date(2026, 5, 4),
            reference_date=date(2026, 5, 1),
            description="Unpaid source",
            is_paid=False,
            owner=self.user,
        )
        rule = TransactionRule.objects.create(
            active=True,
            on_create=False,
            on_update=True,
            name="Create muted copy",
            trigger="True",
            owner=self.user,
        )
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            set_account="account_id",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_amount="amount",
            set_description="'Muted copy'",
            set_is_paid="is_paid",
            set_mute="True",
        )

        run_check_for_transaction_rules_without_worker_wrapper(
            instance_id=source_transaction.id,
            user_id=self.user.id,
            signal="transaction_updated",
        )

        generated_transaction = Transaction.objects.get(description="Muted copy")
        self.assertTrue(generated_transaction.mute)
        self.assertFalse(generated_transaction.is_paid)


class ApplyRulesToAllTransactionsTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="bulk@example.com", password="testpass123"
        )
        currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main Account", currency=currency, owner=self.user
        )

    def _transaction(self, description, day):
        with patch("apps.rules.signals.check_for_transaction_rules.defer"):
            return Transaction.objects.create(
                account=self.account,
                type=Transaction.Type.EXPENSE,
                amount=Decimal("10.00"),
                date=date(2026, 5, day),
                reference_date=date(2026, 5, 1),
                description=description,
                owner=self.user,
            )

    def _rule(self, name, trigger, new_description, **kwargs):
        from apps.rules.models import TransactionRuleAction

        rule = TransactionRule.objects.create(
            name=name, trigger=trigger, owner=self.user, **kwargs
        )
        TransactionRuleAction.objects.create(
            rule=rule, field="description", value=f"'{new_description}'"
        )
        return rule

    def _run(self, **kwargs):
        from apps.rules.tasks import apply_rules_to_all_transactions

        task_func = apply_rules_to_all_transactions.func
        return task_func(user_id=self.user.id, **kwargs)

    def test_runs_rules_on_old_transactions_regardless_of_event_flags(self):
        t1 = self._transaction("old one", 1)
        t2 = self._transaction("old two", 2)
        # Neither on_create nor on_update: would never fire from signals
        self._rule("Rename", "True", "renamed", on_create=False, on_update=False)

        result = self._run()

        self.assertEqual(result, {"processed": 2, "failed": 0})
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.description, "renamed")
        self.assertEqual(t2.description, "renamed")

    def test_only_selected_rules_run_and_trigger_is_respected(self):
        match = self._transaction("coffee", 1)
        other = self._transaction("rent", 2)
        selected = self._rule("A", "'coffee' in description", "A applied")
        self._rule("B", "True", "B applied")

        self._run(rule_ids=[selected.id])

        match.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(match.description, "A applied")
        self.assertEqual(other.description, "rent")

    def test_inactive_rules_are_skipped(self):
        t = self._transaction("keep", 1)
        self._rule("Off", "True", "changed", active=False)

        self._run()

        t.refresh_from_db()
        self.assertEqual(t.description, "keep")

    def test_preview_counts_matches_without_changing_anything(self):
        from apps.common.middleware.thread_local import (
            write_current_user,
            delete_current_user,
        )
        from apps.rules.tasks import preview_rules_on_all_transactions

        self._transaction("coffee", 1)
        self._transaction("coffee beans", 2)
        t3 = self._transaction("rent", 3)
        rule = self._rule(
            "Coffee", "'coffee' in description", "x", on_create=False
        )

        write_current_user(self.user)
        try:
            preview = preview_rules_on_all_transactions(user_id=self.user.id)
            # preview must not clear the current user
            from apps.common.middleware.thread_local import get_current_user

            self.assertEqual(get_current_user(), self.user)
        finally:
            delete_current_user()

        self.assertEqual(preview["total"], 3)
        self.assertEqual(preview["failed"], 0)
        self.assertEqual(preview["rules"][0]["rule"], rule)
        self.assertEqual(preview["rules"][0]["matches"], 2)
        t3.refresh_from_db()
        self.assertEqual(t3.description, "rent")
        self.assertFalse(Transaction.objects.filter(description="x").exists())
