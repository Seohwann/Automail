"""발송 에이전트(단계).

검증된 이메일 + 작성된 초안을 Gmail API 로 발송한다.
실제 반복 발송 루프는 graph.py 의 send_node 가 담당하고,
여기서는 단일 발송 한 건을 책임진다.
"""
from agents.google_clients import send_email


def send_one(creds, company: dict, sender_name: str, sender_email: str,
             attachment_path=None) -> str:
    """업체 한 건을 발송하고 Gmail 메시지 ID 를 반환."""
    result = send_email(
        creds,
        company["email"],
        company["subject"],
        company["body"],
        sender_name=sender_name,
        sender_email=sender_email,
        attachment_path=attachment_path,
    )
    return result["id"]
