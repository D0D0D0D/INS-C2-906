# INS-C2-906 — Unit tests: PIIVault tokenization + claimant_key derivation (design §6-1)

from src.services.pii_vault import PIIVault, derive_claimant_key, detect_pii_extended


class TestPIIVault:
    def test_tokenize_masks_email(self):
        vault = PIIVault()
        result = vault.tokenize("Contact: taro.yamada@example.com for details")
        assert "taro.yamada@example.com" not in result
        assert "[EMAIL_1]" in result

    def test_tokenize_masks_policy_number(self):
        vault = PIIVault()
        result = vault.tokenize("Policy number AB123456789 is active")
        assert "AB123456789" not in result
        assert "POLICY_NO" in result

    def test_resolve_reverses_tokenize(self):
        vault = PIIVault()
        original = "Contact: taro.yamada@example.com"
        tokenized = vault.tokenize(original)
        resolved = vault.resolve(tokenized)
        assert resolved == original

    def test_resolve_never_called_by_default_state_is_isolated_per_request(self):
        """Each PIIVault instance is request-scoped — tokens from one vault
        must not resolve against another (design §6-1)."""
        vault1 = PIIVault()
        vault2 = PIIVault()
        tokenized = vault1.tokenize("taro.yamada@example.com")
        resolved_wrong_vault = vault2.resolve(tokenized)
        # vault2 has no knowledge of vault1's token -> unresolved passthrough
        assert resolved_wrong_vault == tokenized

    def test_repeated_value_reuses_same_token(self):
        vault = PIIVault()
        text = "taro.yamada@example.com appears twice: taro.yamada@example.com"
        result = vault.tokenize(text)
        assert result.count("[EMAIL_1]") == 2

    def test_medical_content_not_tokenized(self):
        """design §6-1: medical/injury descriptive content is intentionally
        left untouched — only direct identifiers are tokenized."""
        vault = PIIVault()
        text = "患者は頭部を負傷し通院加療を要する"
        result = vault.tokenize(text)
        assert result == text

    def test_resolve_handles_nested_dict_and_list(self):
        vault = PIIVault()
        tokenized_email = vault.tokenize("taro.yamada@example.com")
        nested = {"a": [tokenized_email, {"b": tokenized_email}]}
        resolved = vault.resolve(nested)
        assert resolved["a"][0] == "taro.yamada@example.com"
        assert resolved["a"][1]["b"] == "taro.yamada@example.com"


class TestSupplementaryDetection:
    """Regression tests for the supplementary detection layer (detect_pii_extended).

    Fixes two known gaps in the vendored shared.security.pii_detector:
      (1) no Japanese personal-name detection,
      (2) trailing-\\b patterns (email/phone) fail when immediately followed
          by Japanese text.
    Without this layer, an untokenized insured-person name would flow through
    the graph and appear in the opinion draft output.
    """

    def test_japanese_honorific_name_and_email_tokenized(self):
        vault = PIIVault()
        result = vault.tokenize("被保険者 田中太郎様、連絡先 taro@example.com。")
        assert "田中太郎" not in result
        assert "taro@example.com" not in result
        assert "[PERSON_1]" in result
        assert "[EMAIL_1]" in result

    def test_email_immediately_followed_by_japanese_tokenized(self):
        # bare detect_pii misses this: its email pattern's trailing \b fails
        # when the next char is Japanese (both are Unicode word chars).
        vault = PIIVault()
        result = vault.tokenize("連絡先はtaro@example.comにも共有済み")
        assert "taro@example.com" not in result
        assert "[EMAIL_1]" in result

    def test_phone_immediately_followed_by_japanese_tokenized(self):
        vault = PIIVault()
        result = vault.tokenize("電話番号080-1234-5678です")
        assert "080-1234-5678" not in result
        assert "[PHONE_1]" in result

    def test_policy_number_immediately_followed_by_japanese_tokenized(self):
        # the template's own former \b-delimited policy pattern shared the
        # same defect family — fixed with lookarounds.
        vault = PIIVault()
        result = vault.tokenize("証券番号AB123456789です")
        assert "AB123456789" not in result
        assert "[POLICY_NO_1]" in result

    def test_resolve_round_trip_japanese_sentence(self):
        vault = PIIVault()
        original = "被保険者 田中太郎様（連絡先 taro@example.com。電話 080-1234-5678です）"
        assert vault.resolve(vault.tokenize(original)) == original

    def test_no_double_tokenization_when_both_layers_match(self):
        # ASCII-delimited email is found by BOTH detect_pii and the
        # supplementary pattern — overlap dedup must yield exactly one token.
        vault = PIIVault()
        result = vault.tokenize("Contact: taro.yamada@example.com for details")
        assert result.count("[EMAIL_1]") == 1
        assert "taro.yamada@example.com" not in result

    def test_medical_content_still_not_tokenized(self):
        # supplementary patterns must not over-redact medical narrative
        # (design §6-1 — needed for Lv2 checks).
        vault = PIIVault()
        text = "患者は頭部を負傷し通院加療を要する"
        assert vault.tokenize(text) == text

    def test_known_limit_bare_name_without_honorific_not_detected(self):
        # Documented detection limit (docs/07 Troubleshooting): a bare
        # Japanese name with no honorific suffix is NOT detected by either
        # layer. This test pins the limit so a future silent behavior change
        # is caught either way.
        findings = detect_pii_extended("被保険者 田中太郎 は通院中")
        assert findings == []


class TestClaimantKeyDerivation:
    def test_deterministic_same_salt_same_identifier(self):
        key1 = derive_claimant_key("salt-1", "claimant-abc")
        key2 = derive_claimant_key("salt-1", "claimant-abc")
        assert key1 == key2

    def test_different_salt_yields_different_key(self):
        key1 = derive_claimant_key("salt-1", "claimant-abc")
        key2 = derive_claimant_key("salt-2", "claimant-abc")
        assert key1 != key2

    def test_one_way_key_does_not_contain_raw_identifier(self):
        key = derive_claimant_key("salt-1", "claimant-abc")
        assert "claimant-abc" not in key
