from app.models import SlackConversation, may_use_slack_conversation


def test_public_channel_can_be_used_without_membership():
    conversation = SlackConversation("C_PUBLIC", False, False, False, frozenset())
    assert may_use_slack_conversation(requester_id="U_REQUESTER", conversation=conversation)


def test_private_channel_is_hidden_when_requester_is_not_member():
    conversation = SlackConversation("G_PRIVATE", True, False, False, frozenset({"U_OTHER"}))
    assert not may_use_slack_conversation(requester_id="U_REQUESTER", conversation=conversation)


def test_direct_message_requires_requester_membership():
    conversation = SlackConversation("D_DM", True, True, False, frozenset({"U_REQUESTER", "U_OTHER"}))
    assert may_use_slack_conversation(requester_id="U_REQUESTER", conversation=conversation)
