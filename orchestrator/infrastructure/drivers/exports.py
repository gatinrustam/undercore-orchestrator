"""Protocol-owned export policies; QR rendering is a shared mechanical operation."""

import base64
import io
import re
import segno
from orchestrator.domain.contracts import ExportDocument
from orchestrator.domain.models import OrchestratorError


def qr_document(content):
    payload = content.encode("utf-8")
    # QR version 40, byte mode, ECC M. Never truncate credentials to make them fit.
    if len(payload) > 2331:
        raise OrchestratorError("qr_unavailable", 422)
    try:
        qr = segno.make_qr(payload, mode="byte", error="m", boost_error=False)
        output = io.BytesIO()
        qr.save(output, kind="png", scale=6, border=4)
    except segno.DataOverflowError:
        raise OrchestratorError("qr_unavailable", 422) from None
    return ExportDocument(
        format="qr",
        media_type="image/png",
        extension="png",
        encoding="base64",
        data=base64.b64encode(output.getvalue()).decode("ascii"),
    )


class DocumentExports:
    formats = ()
    file_formats = ()

    def export(self, fetch, format, qr_content_format="conf"):
        if format not in self.formats:
            raise OrchestratorError("unsupported_format", 422)
        source = qr_content_format if format == "qr" else format
        if source not in self.file_formats:
            raise OrchestratorError("unsupported_format", 422)
        content = fetch(source)
        if not isinstance(content, str) or len(content.encode("utf-8")) > 65536 or "\0" in content:
            raise OrchestratorError("node_response_invalid", 503)
        self.validate(source, content)
        if format == "qr":
            return qr_document(content)
        return ExportDocument(
            format=format,
            media_type="application/octet-stream",
            extension="conf" if format == "conf" else "vpn",
            encoding="utf-8",
            data=content,
        )


class WireGuardExports(DocumentExports):
    """Export policy only; does not enable a WireGuard lifecycle driver."""

    formats = ("conf", "qr")
    file_formats = ("conf",)

    def validate(self, source, content):
        if source != "conf" or "[Interface]" not in content or "[Peer]" not in content:
            raise OrchestratorError("node_response_invalid", 503)


class AmneziaExports(DocumentExports):
    formats = ("conf", "amnezia-vpn", "qr")
    file_formats = ("conf", "amnezia-vpn")

    def validate(self, source, content):
        if source == "conf":
            WireGuardExports().validate(source, content)
        elif not re.fullmatch(r"vpn://[A-Za-z0-9_-]+", content):
            raise OrchestratorError("node_response_invalid", 503)
