"""Problems the person using the browser can read about and act on."""


class GeminiError(Exception):
    """A failure with a short title and one sentence of detail, both written for the person browsing."""

    def __init__(self, title: str, detail: str) -> None:
        super().__init__(f"{title}: {detail}")
        self.title = title
        self.detail = detail


class CertificateChangedError(GeminiError):
    """A server presented a different certificate than the one saved the first time it was visited."""

    def __init__(
        self,
        host_key: str,
        previous_fingerprint: str,
        new_fingerprint: str,
        first_seen: str,
    ) -> None:
        super().__init__(
            "This site's certificate has changed",
            f"{host_key} now presents a different certificate than the one saved on {first_seen[:10]}. "
            "That is normal when the site renews its certificate, and also what an impostor would look like.",
        )
        self.host_key = host_key
        self.previous_fingerprint = previous_fingerprint
        self.new_fingerprint = new_fingerprint
        self.first_seen = first_seen
