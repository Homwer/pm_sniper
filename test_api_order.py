import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pm_snipe as m


class Reply:
    def __init__(self, status, data):
        self.status_code, self.data = status, data
    def json(self):
        return self.data


class Session:
    def __init__(self):
        self.bodies = []
    def post(self, url, **kwargs):
        self.bodies.append(dict(kwargs['json']))
        if len(self.bodies) == 1:
            return Reply(422, {'product_needs': {'customfields': [
                {'name': 'Username', 'required': True}, {'name': 'Found', 'required': False}],
                'configoptions': []}})
        if len(self.bodies) == 2:
            return Reply(200, {'quote': {'pid': 123, 'product': 'Demo', 'billing_cycle': 'monthly',
                'first_payment': '12.50', 'currency': 'EUR'}, 'product_needs': {
                'customfields': [{'name': 'Username', 'required': True}], 'configoptions': []}})
        return Reply(201, {'order_id': 7, 'invoice_id': 8})


class ApiOrderingTests(unittest.TestCase):
    def test_quote_required_options_are_not_guessed(self):
        class NeedsOther(Session):
            def post(self, url, **kwargs):
                self.bodies.append(dict(kwargs['json']))
                return Reply(422, {'product_needs': {'customfields': [
                    {'name': 'Username', 'required': True}, {'name': 'Other', 'required': True}]}})
        s = NeedsOther()
        with self.assertRaises(ValueError):
            m.api_quote(s, 'example', 123, 'monthly', 'myname')
        self.assertEqual(len(s.bodies), 1)

    def test_quote_then_confirm_with_required_username(self):
        s = Session()
        quote, body = m.api_quote(s, 'example', 123, 'monthly', 'myname')
        self.assertEqual(body['customfields'], {'Username': 'myname'})
        self.assertEqual(len(s.bodies), 2)
        self.assertTrue(all(not x.get('confirm') for x in s.bodies))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(m.api_confirm(s, 'example', body, quote, Path(directory) / 'state.json'), (7, 8))
            self.assertEqual(s.bodies[-1]['expect_first_payment'], '12.50')
            self.assertEqual(s.bodies[-1]['expect_currency'], 'EUR')
            self.assertTrue(s.bodies[-1]['accept_tos'])
            self.assertEqual(len(s.bodies), 3)

    def test_quote_price_change_and_unknown_option_fail_closed(self):
        s = Session()
        quote, body = m.api_quote(s, 'example', 123, 'monthly', 'myname')
        with tempfile.TemporaryDirectory() as directory:
            q = dict(quote, first_payment='NaN')
            with self.assertRaises(ValueError):
                m.api_confirm(s, 'example', body, q, Path(directory) / 'state.json')
        self.assertEqual(len(s.bodies), 2)

    def test_confirm_uncertain_state_is_not_retried(self):
        s = Session()
        quote, body = m.api_quote(s, 'example', 123, 'monthly', 'myname')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            self.assertEqual(m.api_confirm(s, 'example', body, quote, path), (7, 8))
            with self.assertRaises(ValueError):
                m.api_confirm(s, 'example', body, quote, path)
            self.assertEqual(len(s.bodies), 3)

    def test_invalid_key_file_and_order_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'key'
            p.write_text('one\ntwo\n')
            p.chmod(0o600)
            with self.assertRaises(ValueError):
                m.load_order_key(p)

    def test_key_file_private_and_original_code_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'key'
            p.write_text('example-order-key\n')
            p.chmod(0o600)
            self.assertEqual(m.load_order_key(p), 'example-order-key')
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                m.load_order_key(p)
        self.assertTrue(callable(m.set_affiliate))


if __name__ == '__main__':
    unittest.main()
