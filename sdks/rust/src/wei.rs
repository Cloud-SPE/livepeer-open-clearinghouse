//! Serde helpers for `*_wei` amounts on the LOC wire.
//!
//! The gateway emits every wei amount as a decimal integer **string**
//! (`"12345678901234567890"`), never a JSON number, because values may
//! exceed `u64` and JSON numbers lose precision past 2^53 in most
//! decoders. This module parses that string form into `u128`, still
//! accepts a bare non-negative JSON integer emitted by legacy gateways,
//! and always serializes back to the string form.
//!
//! Use with `#[serde(deserialize_with = "wei::deserialize_wei")]` (or
//! `deserialize_opt_wei` together with `#[serde(default)]` for nullable
//! or absent fields), and `serialize_with = "wei::serialize_wei"` /
//! `serialize_opt_wei` on any struct that goes back out on the wire.

use std::fmt;

use serde::de::{self, Deserializer, Unexpected, Visitor};
use serde::Serializer;

/// Parse a wei amount from a decimal integer string or a legacy bare
/// JSON integer. Rejects negatives, floats, `null`, and any string that
/// is not purely ASCII digits.
pub fn deserialize_wei<'de, D>(d: D) -> Result<u128, D::Error>
where
    D: Deserializer<'de>,
{
    d.deserialize_any(WeiVisitor)
}

/// [`deserialize_wei`] for nullable fields: `null` becomes `None`.
///
/// Pair with `#[serde(default)]` so an absent field also reads as `None`;
/// serde does not apply its `Option` missing-field rule once a custom
/// `deserialize_with` is attached.
pub fn deserialize_opt_wei<'de, D>(d: D) -> Result<Option<u128>, D::Error>
where
    D: Deserializer<'de>,
{
    d.deserialize_option(OptWeiVisitor)
}

/// Emit a wei amount as its decimal integer string.
pub fn serialize_wei<S>(value: &u128, s: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    s.serialize_str(&value.to_string())
}

/// [`serialize_wei`] for nullable fields: `None` becomes `null`.
pub fn serialize_opt_wei<S>(value: &Option<u128>, s: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    match value {
        Some(v) => s.serialize_str(&v.to_string()),
        None => s.serialize_none(),
    }
}

/// Parse a wei amount already held in a [`serde_json::Value`]. Same
/// acceptance rules as [`deserialize_wei`].
#[must_use]
pub fn from_value(value: &serde_json::Value) -> Option<u128> {
    deserialize_wei(value).ok()
}

struct WeiVisitor;

impl Visitor<'_> for WeiVisitor {
    type Value = u128;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a wei amount as a decimal integer string")
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<u128, E> {
        parse_digits(v).ok_or_else(|| E::invalid_value(Unexpected::Str(v), &self))
    }

    fn visit_u64<E: de::Error>(self, v: u64) -> Result<u128, E> {
        Ok(u128::from(v))
    }

    fn visit_u128<E: de::Error>(self, v: u128) -> Result<u128, E> {
        Ok(v)
    }

    fn visit_i64<E: de::Error>(self, v: i64) -> Result<u128, E> {
        u128::try_from(v).map_err(|_| E::invalid_value(Unexpected::Signed(v), &self))
    }

    fn visit_i128<E: de::Error>(self, v: i128) -> Result<u128, E> {
        u128::try_from(v).map_err(|_| E::custom(format!("negative wei amount {v}")))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<u128, E> {
        // A bare JSON number wide enough to land here has already lost
        // precision in the decoder; the string form is the only exact one.
        Err(E::invalid_value(Unexpected::Float(v), &self))
    }
}

struct OptWeiVisitor;

impl<'de> Visitor<'de> for OptWeiVisitor {
    type Value = Option<u128>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a wei amount as a decimal integer string, or null")
    }

    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_some<D: Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        deserialize_wei(d).map(Some)
    }
}

fn parse_digits(s: &str) -> Option<u128> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse::<u128>().ok()
}

#[cfg(test)]
mod tests {
    use serde::{Deserialize, Serialize};
    use serde_json::json;

    #[derive(Debug, PartialEq, Deserialize, Serialize)]
    struct Amount {
        #[serde(
            deserialize_with = "super::deserialize_wei",
            serialize_with = "super::serialize_wei"
        )]
        value_wei: u128,
    }

    #[derive(Debug, PartialEq, Deserialize, Serialize)]
    struct MaybeAmount {
        #[serde(
            default,
            deserialize_with = "super::deserialize_opt_wei",
            serialize_with = "super::serialize_opt_wei"
        )]
        value_wei: Option<u128>,
    }

    #[test]
    fn string_beyond_f64_precision_round_trips_exactly() {
        // 1.2e19 sits past 2^53, where JSON-number decoders lose digits.
        let parsed: Amount =
            serde_json::from_str(r#"{"value_wei":"12345678901234567890"}"#).unwrap();
        assert_eq!(parsed.value_wei, 12_345_678_901_234_567_890_u128);
        assert_eq!(
            serde_json::to_string(&parsed).unwrap(),
            r#"{"value_wei":"12345678901234567890"}"#
        );
    }

    #[test]
    fn beyond_u64_round_trips_exactly() {
        let text = format!(r#"{{"value_wei":"{}"}}"#, u128::MAX);
        let parsed: Amount = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed.value_wei, u128::MAX);
        assert_eq!(serde_json::to_string(&parsed).unwrap(), text);
    }

    #[test]
    fn legacy_bare_integer_still_parses() {
        let parsed: Amount = serde_json::from_value(json!({"value_wei": 100_000u64})).unwrap();
        assert_eq!(parsed.value_wei, 100_000);
        let parsed: Amount = serde_json::from_str(r#"{"value_wei":0}"#).unwrap();
        assert_eq!(parsed.value_wei, 0);
    }

    #[test]
    fn option_accepts_null_missing_string_and_number() {
        let null: MaybeAmount = serde_json::from_str(r#"{"value_wei":null}"#).unwrap();
        assert_eq!(null.value_wei, None);
        let missing: MaybeAmount = serde_json::from_str("{}").unwrap();
        assert_eq!(missing.value_wei, None);
        let text: MaybeAmount = serde_json::from_str(r#"{"value_wei":"7"}"#).unwrap();
        assert_eq!(text.value_wei, Some(7));
        let legacy: MaybeAmount = serde_json::from_str(r#"{"value_wei":7}"#).unwrap();
        assert_eq!(legacy.value_wei, Some(7));
        assert_eq!(
            serde_json::to_string(&null).unwrap(),
            r#"{"value_wei":null}"#
        );
        assert_eq!(
            serde_json::to_string(&text).unwrap(),
            r#"{"value_wei":"7"}"#
        );
    }

    #[test]
    fn rejects_malformed_amounts() {
        for body in [
            r#"{"value_wei":""}"#,
            r#"{"value_wei":"+5"}"#,
            r#"{"value_wei":"-5"}"#,
            r#"{"value_wei":" 5"}"#,
            r#"{"value_wei":"1e3"}"#,
            r#"{"value_wei":"0x10"}"#,
            r#"{"value_wei":"340282366920938463463374607431768211456"}"#,
            r#"{"value_wei":-1}"#,
            r#"{"value_wei":1.5}"#,
            r#"{"value_wei":12345678901234567890123}"#,
            r#"{"value_wei":null}"#,
            r#"{"value_wei":true}"#,
        ] {
            assert!(
                serde_json::from_str::<Amount>(body).is_err(),
                "expected {body} to be rejected"
            );
        }
    }

    #[test]
    fn from_value_mirrors_deserializer() {
        assert_eq!(
            super::from_value(&json!("12345678901234567890")),
            Some(12_345_678_901_234_567_890)
        );
        assert_eq!(super::from_value(&json!(42)), Some(42));
        assert_eq!(super::from_value(&json!(null)), None);
        assert_eq!(super::from_value(&json!("nope")), None);
    }
}
