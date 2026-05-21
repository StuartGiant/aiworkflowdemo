// Sensitive URL detection patterns — mirrors config/bookmark_guard.yml
// Each entry: { name, description, pattern (RegExp) }

export const PATTERNS = [
  {
    name: "pii_endpoint",
    description: "URLs pointing to PII data endpoints or personal-data exports",
    pattern: /\/(?:pii|personal[-_]data|user[-_]data|employee[-_]data)(?:\/|$|\?)/i,
  },
  {
    name: "ssn_in_url",
    description: "Social Security Number pattern embedded in a URL",
    pattern: /\b\d{3}[-]\d{2}[-]\d{4}\b/i,
  },
  {
    name: "credit_card_in_url",
    description: "Credit card number pattern embedded in a URL",
    pattern: /\b(?:\d{4}[-\s]){3}\d{4}\b/i,
  },
  {
    name: "internal_hr",
    description: "Internal HR system URLs",
    pattern: /hr\.(?:internal|corp|company)\./i,
  },
  {
    name: "payroll_system",
    description: "Internal payroll or compensation system URLs",
    pattern: /(?:payroll|salary|compensation)\.(?:internal|corp)\./i,
  },
  {
    name: "internal_finance",
    description: "Internal finance, accounting, or treasury portal URLs",
    pattern: /(?:finance|accounting|treasury)\.(?:internal|corp)\./i,
  },
  {
    name: "classified_docs",
    description: "Bookmarks to classified or restricted document repositories",
    pattern: /(?:confidential|classified|sensitive|restricted)\.(?:internal|corp)\./i,
  },
  {
    name: "admin_user_portal",
    description: "Admin portals showing user, employee, or personnel data",
    pattern: /\/(?:admin|superuser|root)\/.*(?:users|employees|personnel)/i,
  },
  {
    name: "bulk_data_export",
    description: "URLs that trigger bulk data exports",
    pattern: /\/export(?:\/|$|\?).*\.(?:csv|xlsx|json|parquet)/i,
  },
  {
    name: "health_records",
    description: "Health or medical record system URLs",
    pattern: /(?:ehr|emr|hipaa|healthrecords?|medicalrecords?)\.(?:internal|corp)\./i,
  },
  {
    name: "netflix",
    description: "Netflix and all its subdomains",
    pattern: /\bnetflix\.com(?:[/?:#]|$)/i,
  },
];

export function matchingPattern(url) {
  return PATTERNS.find((p) => p.pattern.test(url)) ?? null;
}
