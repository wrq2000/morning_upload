"""Контроль префикса темы для всех путей отправки, без сетевых запросов."""
import unittest
from unittest.mock import Mock, patch
from common.mailer import MailSender, SMTPProfile, format_subject, send_mail


class SubjectTests(unittest.TestCase):
    def test_all_process_subjects(self):
        for subject in (
            "Argus Media Reports", "Annual LNG Plan for 2026", "БелАЭС",
            "Tuz Gölü Data", "EPİAŞ данные", "УЭБ — экспорт природного газа",
            "СПбМТСБ", "Цхинвал", "Eurostat", "Вывозные пошлины — нефть и СУГ",
            "ENTSO-E", "ChinaMoney",
            "❌ Ошибка при сборе данных БелАЭС", "ChinaMoney — ошибка",
        ):
            with self.subTest(subject=subject):
                self.assertEqual(format_subject(subject), "134101 " + subject)

    def test_idempotent_and_default(self):
        for subject in ("134101 Тема", "134101: Тема", "134101 — Тема"):
            self.assertEqual(format_subject(subject), subject)
        self.assertEqual(format_subject(" 134101 Тема "), "134101 Тема")
        self.assertEqual(format_subject(), "134101 Отчет системы сбора данных")
        self.assertEqual(format_subject(" "), "134101 Отчет системы сбора данных")
        self.assertEqual(format_subject("1341012 Тема"), "134101 1341012 Тема")

    @patch.dict('os.environ', {}, clear=True)
    def test_primary_and_backup_actual_message(self):
        profiles = [SMTPProfile(name, "smtp.example.com", 587, "a@example.com",
                                "test", "a@example.com", "starttls", 30)
                    for name in ("primary", "backup")]
        primary, backup = Mock(), Mock()
        primary.send_message.return_value = {"b@example.com": (450, b"retry")}
        backup.send_message.return_value = {}
        with patch('common.mailer._profile', side_effect=profiles), \
             patch.dict('os.environ', {'SMTP_BACKUP_SERVER': 'smtp.example.com'}), \
             patch.object(MailSender, '_connect', side_effect=[primary, backup]):
            self.assertTrue(send_mail(subject="Вывозные пошлины — нефть и СУГ",
                                      recipients="a@example.com,b@example.com"))
        for server in (primary, backup):
            msg = server.send_message.call_args.args[0]
            self.assertEqual(str(msg['Subject']), "134101 Вывозные пошлины — нефть и СУГ")
        self.assertEqual(backup.send_message.call_args.kwargs['to_addrs'], ['b@example.com'])

    @patch.dict('os.environ', {}, clear=True)
    def test_direct_sender_and_existing_prefix(self):
        profile = SMTPProfile('primary', 'smtp.example.com', 587, 'a@example.com',
                              'test', 'a@example.com', 'starttls', 30)
        server = Mock()
        server.send_message.return_value = {}
        with patch('common.mailer._profile', return_value=profile), \
             patch.object(MailSender, '_connect', return_value=server):
            self.assertTrue(MailSender().send_mail(subject='134101 УЭБ — ошибка',
                                                   recipients='b@example.com'))
        self.assertEqual(str(server.send_message.call_args.args[0]['Subject']), '134101 УЭБ — ошибка')
