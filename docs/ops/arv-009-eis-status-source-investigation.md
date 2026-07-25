# ARV-009: EIS Procurement Status Source Investigation

## Question

Can the lifecycle status (active / completed / cancelled) of an EIS procurement be
determined from the export XML obtained via `getDocsByOrgRegion` / `getDocsIP`
(Result B — export XML only), or is an external status source required?

## Method

1. **Schema inventory**: Parse all XML elements in a set of 38 live EIS export XMLs
   (from 5 archives covering 7 days in Tyumen, 178 unique procurements).
   Search for any element whose local name or path matches `status`, `state`,
   `phase`, `procurementState`, `publicationStatus`, or similar status-related
   patterns — across all namespaces.

2. **External API comparison**: Compare the XML schema inventory against the
   `getDocsIP` SOAP response schema, which is known to carry `<status>` in the
   `EPtypes` namespace.

3. **Manual confirmation**: Visually inspect all 38 XMLs for any status-bearing
   element not caught by the automated inventory.

## Results

### A. Schema inventory: no status element found in export XML

The element-path frequency table (38 files, 27 677 element occurrences) contains
**zero** matches for any of the following element-local-name patterns:

- `status`
- `state`
- `phase`
- `procurementState`
- `publicationStatus`
- `lifecycleStatus`
- `notificationStatus`

The `status_like_elements` dict in the schema inventory is empty.

### B. Root namespace confirms export format

All 38 XMLs use the root namespace `http://zakupki.gov.ru/oos/export/1` with
local name `export`. The EPtypes namespace
(`http://zakupki.gov.ru/oos/EPtypes/1`) is present in sub-elements, and the
EPtypes XSD *defines* a `<status>` element — but it is never populated in
export ZIP content.

### C. Deadline field confirmed

The only reliable deadline field is `collectingInfo/endDT` at path
`export/epNotificationEF2020/notificationInfo/procedureInfo/collectingInfo/endDT`.
Coverage: 38/38 XMLs (100%). `startDT` is also at 100%.

No other deadline-like fields exist (no `applicationDeadline`, `submissionDeadline`,
`bidDeadline`).

### D. Only one document type

All 38 XMLs are `epNotificationEF2020` (44-ФЗ notices). No cancellation,
completion, or protocol documents were found.

## Conclusions

### Result B: Confirmed UNAVAILABLE

**Export ZIP XMLs obtained via `getDocsByOrgRegion` / `getDocsIP` do not contain
procurement lifecycle status.** The status element exists in the `EPtypes` XSD
namespace but is not present in any live export XML across the sampled set.

Therefore:

- Active/completed/cannot classification **cannot** be performed from export XML alone.
- **An external status source is required** to classify procurements by lifecycle status.
- The only confirmed source of `status` within EIS is the SOAP search API
  (`getDocsIP` response), which wraps the same XML content with additional
  metadata — including `<status>`.

### Impact

- `status_classification_coverage_percent` remains 0% when only export XML is used.
- The status classification coverage gate (95%) cannot be passed without a
  separate status source (GetDocsIP metadata or a pre-classified manifest).
- The `excluded_unmapped` counter (178/178 = 100%) confirms every procurement
  lands in "unknown status" when export-XML-only parsing is used.

### Marker

`ARV-009C1_EXACT_ACTIVE_SET_REQUIRES_EXTERNAL_STATUS_SOURCE`
