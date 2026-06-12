from __future__ import annotations

import unittest

from wtup.permissions import admin_target_allows_sender, normalize_user_id


class AdminTargetPermissionTest(unittest.TestCase):
    def test_numeric_admin_target_allows_matching_sender(self) -> None:
        self.assertTrue(admin_target_allows_sender(["123456"], sender_id="123456"))

    def test_numeric_admin_target_rejects_other_sender(self) -> None:
        self.assertFalse(admin_target_allows_sender(["123456"], sender_id="654321"))

    def test_private_origin_admin_target_allows_matching_event_origin(self) -> None:
        origin = "aiocqhttp:private:123456"

        self.assertTrue(admin_target_allows_sender([origin], event_origin=origin))

    def test_private_origin_admin_target_allows_group_command_from_same_sender(self) -> None:
        self.assertTrue(admin_target_allows_sender(["aiocqhttp:private:123456"], sender_id="123456"))

    def test_friend_origin_admin_target_allows_group_command_from_same_sender(self) -> None:
        self.assertTrue(admin_target_allows_sender(["aiocqhttp:FriendMessage:123456"], sender_id="123456"))

    def test_group_origin_digits_do_not_grant_sender_permission(self) -> None:
        self.assertFalse(admin_target_allows_sender(["aiocqhttp:group:123456"], sender_id="123456"))

    def test_group_origin_exact_match_does_not_grant_permission(self) -> None:
        origin = "aiocqhttp:group:123456"

        self.assertFalse(admin_target_allows_sender([origin], event_origin=origin))

    def test_empty_admin_targets_reject_sender(self) -> None:
        self.assertFalse(admin_target_allows_sender([], sender_id="123456"))

    def test_normalize_user_id_only_accepts_plain_digits(self) -> None:
        self.assertEqual(normalize_user_id("123456"), "123456")
        self.assertEqual(normalize_user_id("qq:123456"), "")
