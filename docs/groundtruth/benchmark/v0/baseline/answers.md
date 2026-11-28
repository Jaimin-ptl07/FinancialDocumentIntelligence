# Answer-quality eval

- **benchmark_version**: `v0`
- **git_commit**: `594fa8483b8c5d264c3bf38073cc6cdfbd559ad2`
- **endpoint**: `http://localhost:9514`
- **mode**: `answers`
- **started_at**: `2026-11-26T14:43:28+00:00`
- **judge**: `azure-openai:gpt-4.1-invoice-processing`
- **resumed**: `{'from': '..\\eval-runs\\baseline\\report.json', 'items': 55}`
- **items run**: 96 of 96

| kind | run | correct | wrong | failed | accuracy |
|---|---:|---:|---:|---:|---:|
| calculation | 10 | 0 | 10 | 0 | 0.0% |
| cross_company | 6 | 0 | 6 | 0 | 0.0% |
| fact_lookup | 59 | 5 | 54 | 0 | 8.5% |
| research | 11 | 3 | 8 | 0 | 27.3% |
| unanswerable | 10 | 6 | 4 | 0 | 60.0% |
| **overall** | 96 | 14 | 82 | 0 | 14.6% |

## Task completion

Completed = every gold value stated AND at least one citation on a gold evidence page. Only citations carrying a `doc_key` and page can be valid; today's /ask table and concept citations carry neither, so they never count. An unanswerable item has no gold evidence: it completes when it asserts no figure and says the information was not found, and its support is always 0.

| kind | run | completed | completion | supported | evidence support | citations | valid | citation validity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| calculation | 10 | 0 | 0.0% | 4 | 40.0% | 7526 | 15 | 0.2% |
| cross_company | 6 | 0 | 0.0% | 2 | 33.3% | 2643 | 3 | 0.1% |
| fact_lookup | 59 | 2 | 3.4% | 27 | 45.8% | 32635 | 60 | 0.2% |
| research | 11 | 2 | 18.2% | 8 | 72.7% | 31625 | 102 | 0.3% |
| unanswerable | 10 | 6 | 60.0% | 0 | 0.0% | 4547 | 0 | 0.0% |
| **overall** | 96 | 10 | 10.4% | 41 | 42.7% | 78976 | 180 | 0.2% |

## Not correct

| item | status | reason |
|---|---|---|
| `calculation:TCS-FY2025-26-consolidated:net_profit_margin` | wrong | 18.22 percent not stated |
| `calculation:TCS-FY2025-26-consolidated:effective_tax_rate` | wrong | 24.48 percent not stated |
| `calculation:TCS-FY2025-26-standalone:net_profit_margin` | wrong | 21.26 percent not stated |
| `calculation:TCS-FY2025-26-consolidated:total_assets` | wrong | 182372 INR crore not stated |
| `calculation:TCS-FY2025-26-consolidated:total_liabilities` | wrong | 73894 INR crore not stated |
| `calculation:TCS-FY2025-26-consolidated:current_ratio` | wrong | 2.228 ratio not stated |
| `calculation:TCS-FY2025-26-consolidated:total_lease_liabilities` | wrong | 11283 INR crore not stated |
| `calculation:RIL-FY2025-26-consolidated:revenue_net_of_gst` | wrong | 1.07568e+06 INR crore not stated |
| `calculation:REMC-FY2024-25:net_profit_margin` | wrong | 56.46 percent not stated |
| `calculation:REMC-FY2024-25:effective_tax_rate` | wrong | 21.96 percent not stated |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_income` | wrong | 271423 INR crore not stated; 1.10464e+06 INR crore not stated |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_expenses` | wrong | 201410 INR crore not stated; 981475 INR crore not stated |
| `cross_company:TCS-RIL-FY2025-26-consolidated:other_comprehensive_income` | wrong | 2750 INR crore not stated; -11547 INR crore not stated |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_comprehensive_income` | wrong | 52204 INR crore not stated; 84207 INR crore not stated |
| `cross_company:TCS-RIL-FY2025-26-consolidated:basic_eps` | wrong | 136.01 INR not stated; 59.69 INR not stated |
| `cross_company:TCS-FY2025-26-REMC-FY2024-25:basic_eps` | wrong | 136.01 INR not stated; 7.55 INR not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:CT` | wrong | -2681.75 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:DT` | wrong | 450.34 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:EPSB` | wrong | 7.55 INR not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:EPSD` | wrong | 7.55 INR not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PATR` | wrong | 7931.09 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PBMI` | wrong | 7931.09 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PBTR` | wrong | 10162.5 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:TOTEXPR` | wrong | 3884.41 INR lakh not stated |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:TOTRNOTINR` | wrong | 14046.9 INR lakh not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:CT` | wrong | 9736 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:DT` | wrong | 17816 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSB` | wrong | 59.69 INR not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSD` | wrong | 59.69 INR not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:GST` | wrong | 100244 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:OTCI` | wrong | -11547 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:PBMI` | wrong | 84207 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSP` | wrong | 1.04359e+06 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSS` | wrong | 132330 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:SOP` | wrong | 144 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | wrong | 981475 INR crore not stated |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | wrong | 1.10464e+06 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:INVT` | wrong | 29 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETDTA` | wrong | 4465 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETGW` | wrong | 9108 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:RGHTUA` | wrong | 11027 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:TOTCAR` | wrong | 135705 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:TOTNCR` | wrong | 46667 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTOTEQ` | wrong | 106878 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:LTOLR` | wrong | 9453 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:NETDTL` | wrong | 1205 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:STLL` | wrong | 1830 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:STTRP` | wrong | 14808 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:TOTCLR` | wrong | 60914 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:TOTNCLR` | wrong | 12980 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:CT` | wrong | 16388 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:DT` | wrong | -355 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSB` | wrong | 136.01 INR not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSD` | wrong | 136.01 INR not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:OTCI` | wrong | 2750 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PB3NTAX` | wrong | 70013 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PBMI` | wrong | 52204 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PBTR` | wrong | 65487 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | wrong | 201410 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | wrong | 271423 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:CT` | wrong | 14178 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:DT` | wrong | -468 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSB` | wrong | 135.7 INR not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSD` | wrong | 135.7 INR not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:OTCI` | wrong | -600 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PB3NTAX` | wrong | 66873 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PBMI` | wrong | 48496 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PBTR` | wrong | 62806 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTEXPR` | wrong | 164101 INR crore not stated |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTRNOTINR` | wrong | 230974 INR crore not stated |
| `research:TCS-FY2025-26-consolidated:other_income_note` | wrong | 3854 INR crore not stated; 2059 INR crore not stated |
| `research:TCS-FY2025-26-consolidated:tax_rate_reconciliation` | wrong | 25.17 percent not stated; 16481 INR crore not stated; 1878 INR crore not stated |
| `research:TCS-FY2025-26-consolidated:restructuring_exceptional_item` | wrong | 1268 INR crore not stated |
| `research:TCS-FY2025-26-consolidated:talent_retention_and_learning` | wrong | 13.7 percent not stated |
| `research:RIL-FY2025-26-consolidated:other_income_note` | wrong | 13302 INR crore not stated; 9100 INR crore not stated |
| `research:RIL-FY2025-26-consolidated:tax_rate_reconciliation` | wrong | 25.168 percent not stated; 22.37 percent not stated; 30997 INR crore not stated |
| `research:REMC-FY2024-25-unknown:other_income_note` | wrong | 544.35 INR lakh not stated; 91.81 INR lakh not stated |
| `research:REMC-FY2024-25-unknown:revenue_from_operations_note` | wrong | 13053 INR lakh not stated; 1348.45 INR lakh not stated |
| `unanswerable:company-not-in-corpus:hindustan-unilever-eps` | wrong | no statement that the information was not found |
| `unanswerable:year-not-covered:REMC-FY2025-26-profit` | wrong | asserted a figure the Reports do not hold: ₹52,820 crore |
| `unanswerable:basis-not-reported:REMC-FY2024-25-consolidated-profit` | wrong | asserted a figure the Reports do not hold: ₹48,797 crore |
| `unanswerable:concept-not-disclosed:RIL-FY2025-26-extraordinary-items` | wrong | no statement that the information was not found |

## Correct but not completed

No citation on a gold evidence page.

| item | citations | valid |
|---|---:|---:|
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTEQNR` | 214 | 0 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PATR` | 129 | 0 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PATR` | 129 | 0 |
| `research:REMC-FY2024-25-unknown:income_decline_reasons` | 15 | 0 |

## LLM judge

An LLM's opinion, not a score: whether each required claim is made by the answer AND supported by the evidence it cited, judged from that evidence alone. Nothing above reads it. A judge error is the judge's failure, left out of the rates; an item that never answered supports no claim.

| kind | items | judge errors | claims | supported | claim support | fully supported | full support |
|---|---:|---:|---:|---:|---:|---:|---:|
| research | 11 | 0 | 31 | 11 | 35.5% | 2 | 18.2% |
| **overall** | 11 | 0 | 31 | 11 | 35.5% | 2 | 18.2% |

| item | unsupported claim | why |
|---|---|---|
| `research:TCS-FY2025-26-consolidated:other_income_note` | Interest income is the largest component of other income. | The evidence does not provide a breakdown of the components of other income or state that interest income is the largest component. |
| `research:TCS-FY2025-26-consolidated:other_income_note` | Interest on financial assets carried at fair value through OCI is the largest component of interest income. | The evidence does not mention interest on financial assets carried at fair value through OCI or its relation to interest income. |
| `research:TCS-FY2025-26-consolidated:other_income_note` | Net foreign exchange was a loss in FY2025-26, against a gain in FY2024-25. | The evidence does not provide information about net foreign exchange gains or losses for FY2025-26 or FY2024-25. |
| `research:TCS-FY2025-26-consolidated:tax_rate_reconciliation` | Tax pertaining to prior years reduced the reported tax expense below the expected amount. | The evidence does not mention tax pertaining to prior years reducing the reported tax expense below the expected amount. |
| `research:TCS-FY2025-26-consolidated:tax_rate_reconciliation` | Tax on income at different rates increased the tax expense. | The evidence does not mention tax on income at different rates increasing the tax expense. |
| `research:TCS-FY2025-26-consolidated:restructuring_exceptional_item` | The Group announced re-structuring initiatives in July 2025. | The evidence does not mention any announcement of re-structuring initiatives in July 2025. |
| `research:TCS-FY2025-26-consolidated:restructuring_exceptional_item` | The expenses are termination benefits for associates released from the organisation whose deployment may not be feasible. | The evidence does not specify that the expenses are termination benefits for associates released from the organisation. |
| `research:TCS-FY2025-26-consolidated:talent_retention_and_learning` | The 13.7% figure is voluntary attrition in IT services. | The evidence does not mention a 13.7% voluntary attrition figure for IT services or any specific attrition percentage. |
| `research:TCS-FY2025-26-consolidated:talent_retention_and_learning` | Associates completed a total of 69 million learning hours in FY 2026. | The evidence does not state that associates completed a total of 69 million learning hours in FY 2026 or provide any quantitative learning hours data. |
| `research:TCS-FY2025-26-consolidated:talent_retention_and_learning` | Learning intensity rose 25% year-on-year to 120 hours per associate per year. | The evidence does not mention a 25% year-on-year rise in learning intensity or specify 120 hours per associate per year. |
| `research:RIL-FY2025-26-consolidated:other_income_note` | Interest is the largest component of other income. | The evidence does not include any breakdown or note explaining the components of 'other income', so it cannot confirm if interest is the largest component. |
| `research:RIL-FY2025-26-consolidated:other_income_note` | Interest on bank deposits is the largest component of interest income. | There is no evidence provided about the composition of interest income or whether interest on bank deposits is its largest component. |
| `research:RIL-FY2025-26-consolidated:other_income_note` | Dividend income rose sharply from FY2024-25. | The evidence does not contain any information about dividend income or its change from FY2024-25. |
| `research:RIL-FY2025-26-consolidated:tax_rate_reconciliation` | Additional allowances / deductions were the largest item reducing the current tax provision below the computed tax. | The evidence does not provide details on additional allowances or deductions reducing the current tax provision below the computed tax. |
| `research:RIL-FY2025-26-consolidated:tax_rate_reconciliation` | Utilisation of carried forward losses also reduced the current tax provision. | There is no evidence mentioning utilisation of carried forward losses as a factor reducing the current tax provision. |
| `research:RIL-FY2025-26-consolidated:tax_rate_reconciliation` | The deferred tax provision arose mainly from incremental deferred tax liability on property, plant and equipment and other intangible assets. | The evidence does not specify that the deferred tax provision arose mainly from incremental deferred tax liability on property, plant and equipment and other intangible assets. |
| `research:REMC-FY2024-25-unknown:other_income_note` | Miscellaneous income includes sale of tender documents, penalties or compensation received, and profit on sale of fixed assets. | The evidence does not mention miscellaneous income, sale of tender documents, penalties, compensation received, or profit on sale of fixed assets as a grouped component; only 'Net gain on disposal of property, plant and equipment' is listed separately. |
| `research:REMC-FY2024-25-unknown:revenue_from_operations_note` | Consultancy fee is reported net of Goods and Services Tax. | None of the cited evidence explicitly states that consultancy fee is reported net of Goods and Services Tax. |
| `research:REMC-FY2024-25-unknown:income_decline_reasons` | Generation from the Jaisalmer Wind Power Plant was reduced. | The evidence does not mention the Jaisalmer Wind Power Plant or any reduction in its generation. |
| `research:REMC-FY2024-25-unknown:income_decline_reasons` | RE-RTC bids were non-responsive because the expected CTUIL connectivity period was beyond the bid timelines for project execution. | The evidence does not mention RE-RTC bids or CTUIL connectivity period as reasons for decreased income or profit before tax. |

## Latency and cost

- **cost**: INR 595.4144 (2432040 input + 18742 output tokens)
- **completed**: 10 of 96
- **cost per completed task**: INR 59.5414
- **judge**: INR 34.7040 (140590 input + 1383 output tokens; eval spend, not in cost per completed task)
- **total**: INR 630.1184

Latency is submit to answer, failed items included; it can run up to one poll interval over the job's own time.

| kind | items | P50 latency (s) | P95 latency (s) |
|---|---:|---:|---:|
| calculation | 10 | 14.65 | 31.10 |
| cross_company | 6 | 19.67 | 35.76 |
| fact_lookup | 59 | 14.27 | 19.60 |
| research | 11 | 15.84 | 23.89 |
| unanswerable | 10 | 10.20 | 22.08 |
| **overall** | 96 | 14.38 | 23.89 |

| kind | input tokens | output tokens | cost | completed | cost per completed task |
|---|---:|---:|---:|---:|---:|
| calculation | 322164 | 2492 | INR 78.8813 | 0 | - |
| cross_company | 300833 | 2427 | INR 73.7535 | 0 | - |
| fact_lookup | 1399794 | 10451 | INR 342.3795 | 2 | INR 171.1898 |
| research | 309450 | 2212 | INR 75.5958 | 2 | INR 37.7979 |
| unanswerable | 99799 | 1160 | INR 24.8043 | 6 | INR 4.1340 |
| **overall** | 2432040 | 18742 | INR 595.4144 | 10 | INR 59.5414 |

| item | status | latency (s) | input tokens | output tokens | cost |
|---|---|---:|---:|---:|---:|
| `calculation:TCS-FY2025-26-consolidated:net_profit_margin` | wrong | 31.10 | 21027 | 322 | INR 5.2998 |
| `calculation:TCS-FY2025-26-consolidated:effective_tax_rate` | wrong | 14.65 | 48458 | 236 | INR 11.7330 |
| `calculation:TCS-FY2025-26-standalone:net_profit_margin` | wrong | 16.28 | 21015 | 339 | INR 5.3131 |
| `calculation:TCS-FY2025-26-consolidated:total_assets` | wrong | 14.59 | 42894 | 202 | INR 10.3792 |
| `calculation:TCS-FY2025-26-consolidated:total_liabilities` | wrong | 14.65 | 45557 | 180 | INR 10.9908 |
| `calculation:TCS-FY2025-26-consolidated:current_ratio` | wrong | 12.20 | 20784 | 282 | INR 5.2041 |
| `calculation:TCS-FY2025-26-consolidated:total_lease_liabilities` | wrong | 16.64 | 15840 | 172 | INR 3.9254 |
| `calculation:RIL-FY2025-26-consolidated:revenue_net_of_gst` | wrong | 18.26 | 41094 | 191 | INR 9.9413 |
| `calculation:REMC-FY2024-25:net_profit_margin` | wrong | 14.25 | 22232 | 333 | INR 5.5964 |
| `calculation:REMC-FY2024-25:effective_tax_rate` | wrong | 17.59 | 43263 | 235 | INR 10.4982 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_income` | wrong | 17.68 | 47906 | 428 | INR 11.7843 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_expenses` | wrong | 32.34 | 14636 | 398 | INR 3.8542 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:other_comprehensive_income` | wrong | 17.34 | 74457 | 426 | INR 18.0882 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_comprehensive_income` | wrong | 19.67 | 74880 | 420 | INR 18.1830 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:basic_eps` | wrong | 30.71 | 44535 | 432 | INR 10.9875 |
| `cross_company:TCS-FY2025-26-REMC-FY2024-25:basic_eps` | wrong | 35.76 | 44419 | 323 | INR 10.8564 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:CT` | wrong | 14.38 | 28712 | 204 | INR 7.0129 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:DT` | wrong | 14.46 | 29366 | 166 | INR 7.1321 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:EPSB` | wrong | 13.47 | 14341 | 189 | INR 3.5855 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:EPSD` | wrong | 21.20 | 14334 | 176 | INR 3.5715 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PATR` | wrong | 10.25 | 9888 | 217 | INR 2.5545 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PBMI` | wrong | 12.61 | 29609 | 171 | INR 7.1946 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:PBTR` | wrong | 8.13 | 7979 | 169 | INR 2.0556 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:TOTEXPR` | wrong | 14.27 | 14838 | 181 | INR 3.6960 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:TOTRNOTINR` | wrong | 17.44 | 15759 | 181 | INR 3.9147 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:CT` | wrong | 16.16 | 27916 | 162 | INR 6.7839 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:DT` | wrong | 19.60 | 28083 | 168 | INR 6.8293 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSB` | wrong | 12.94 | 14977 | 181 | INR 3.7290 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSD` | wrong | 17.73 | 16781 | 177 | INR 4.1536 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:GST` | wrong | 14.21 | 39094 | 173 | INR 9.4492 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:OTCI` | wrong | 15.68 | 44722 | 180 | INR 10.7925 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:PBMI` | wrong | 15.74 | 29126 | 168 | INR 7.0770 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSP` | wrong | 19.20 | 27484 | 165 | INR 6.6842 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSS` | wrong | 18.26 | 43099 | 178 | INR 10.4051 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:SOP` | wrong | 12.30 | 28464 | 164 | INR 6.9160 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | wrong | 18.98 | 14763 | 159 | INR 3.6573 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | wrong | 16.34 | 28467 | 182 | INR 6.9338 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:INVT` | wrong | 12.17 | 14848 | 163 | INR 3.6812 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETDTA` | wrong | 17.70 | 30167 | 167 | INR 7.3233 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETGW` | wrong | 12.19 | 11470 | 251 | INR 2.9626 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETOTIA` | correct | 14.23 | 29926 | 217 | INR 7.3136 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:RGHTUA` | wrong | 14.21 | 19236 | 173 | INR 4.7329 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:TOTCAR` | wrong | 17.80 | 15265 | 173 | INR 3.7898 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:TOTNCR` | wrong | 14.47 | 13871 | 189 | INR 3.4739 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTEQ` | correct | 12.35 | 26981 | 162 | INR 6.5619 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTEQNR` | correct | 10.20 | 13757 | 148 | INR 3.4079 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTOTEQ` | wrong | 12.32 | 29565 | 165 | INR 7.1784 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:LTOLR` | wrong | 12.47 | 39759 | 157 | INR 9.5919 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:NETDTL` | wrong | 12.37 | 28178 | 179 | INR 6.8623 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:STLL` | wrong | 14.30 | 42265 | 164 | INR 10.1937 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:STTRP` | wrong | 12.25 | 14725 | 172 | INR 3.6606 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:TOTCLR` | wrong | 13.20 | 42970 | 186 | INR 10.3821 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:TOTNCLR` | wrong | 14.52 | 42861 | 181 | INR 10.3514 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:CT` | wrong | 12.36 | 28698 | 169 | INR 6.9763 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:DT` | wrong | 12.19 | 16505 | 168 | INR 4.0795 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSB` | wrong | 16.43 | 14178 | 190 | INR 3.5478 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSD` | wrong | 14.27 | 15516 | 185 | INR 3.8608 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:OTCI` | wrong | 17.07 | 43854 | 193 | INR 10.5987 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PATR` | correct | 12.16 | 10061 | 164 | INR 2.5453 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PB3NTAX` | wrong | 14.36 | 43749 | 187 | INR 10.5680 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PBMI` | wrong | 14.84 | 29384 | 174 | INR 7.1440 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PBTR` | wrong | 10.14 | 7741 | 166 | INR 1.9962 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | wrong | 16.62 | 27994 | 167 | INR 6.8072 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | wrong | 17.13 | 15905 | 180 | INR 3.9484 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:CT` | wrong | 20.55 | 30288 | 165 | INR 7.3502 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:DT` | wrong | 14.59 | 28458 | 169 | INR 6.9193 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSB` | wrong | 12.19 | 14178 | 187 | INR 3.5449 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSD` | wrong | 12.23 | 14171 | 182 | INR 3.5385 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:OTCI` | wrong | 14.92 | 43192 | 175 | INR 10.4244 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PATR` | correct | 10.47 | 10061 | 152 | INR 2.5339 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PB3NTAX` | wrong | 16.65 | 28701 | 191 | INR 6.9979 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PBMI` | wrong | 12.58 | 15580 | 190 | INR 3.8807 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:PBTR` | wrong | 10.15 | 7741 | 181 | INR 2.0104 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTEXPR` | wrong | 14.28 | 14306 | 175 | INR 3.5639 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTRNOTINR` | wrong | 12.22 | 15887 | 183 | INR 3.9470 |
| `research:TCS-FY2025-26-consolidated:other_income_note` | wrong | 15.84 | 46373 | 148 | INR 11.1542 |
| `research:TCS-FY2025-26-consolidated:tax_rate_reconciliation` | wrong | 14.19 | 15481 | 187 | INR 3.8544 |
| `research:TCS-FY2025-26-consolidated:restructuring_exceptional_item` | wrong | 19.65 | 19356 | 154 | INR 4.7434 |
| `research:TCS-FY2025-26-consolidated:final_dividend_proposed` | correct | 8.14 | 7709 | 138 | INR 1.9620 |
| `research:TCS-FY2025-26-consolidated:talent_retention_and_learning` | wrong | 12.19 | 23499 | 187 | INR 5.7587 |
| `research:RIL-FY2025-26-consolidated:other_income_note` | wrong | 22.58 | 43071 | 153 | INR 10.3747 |
| `research:RIL-FY2025-26-consolidated:tax_rate_reconciliation` | wrong | 23.89 | 55680 | 187 | INR 13.4017 |
| `research:RIL-FY2025-26-consolidated:segment_ebitda_decline` | correct | 19.02 | 30131 | 339 | INR 7.4782 |
| `research:REMC-FY2024-25-unknown:other_income_note` | wrong | 23.64 | 17511 | 167 | INR 4.3175 |
| `research:REMC-FY2024-25-unknown:revenue_from_operations_note` | wrong | 14.51 | 33479 | 348 | INR 8.2819 |
| `research:REMC-FY2024-25-unknown:income_decline_reasons` | correct | 10.17 | 17160 | 204 | INR 4.2693 |
| `unanswerable:company-not-in-corpus:infosys-revenue` | correct | 6.45 | 3488 | 77 | INR 0.9015 |
| `unanswerable:company-not-in-corpus:wipro-profit` | correct | 6.15 | 3488 | 74 | INR 0.8987 |
| `unanswerable:company-not-in-corpus:hdfc-bank-net-interest-income` | correct | 12.29 | 3488 | 79 | INR 0.9034 |
| `unanswerable:company-not-in-corpus:larsen-and-toubro-total-income` | correct | 8.20 | 7119 | 110 | INR 1.7953 |
| `unanswerable:company-not-in-corpus:hindustan-unilever-eps` | wrong | 14.21 | 14598 | 187 | INR 3.6447 |
| `unanswerable:year-not-covered:TCS-FY2027-28-total-income` | correct | 8.20 | 3486 | 89 | INR 0.9125 |
| `unanswerable:year-not-covered:REMC-FY2025-26-profit` | wrong | 10.20 | 10043 | 159 | INR 2.5363 |
| `unanswerable:basis-not-reported:REMC-FY2024-25-consolidated-profit` | wrong | 10.21 | 9876 | 144 | INR 2.4823 |
| `unanswerable:year-not-covered:RIL-FY2027-28-revenue` | correct | 16.28 | 3488 | 88 | INR 0.9120 |
| `unanswerable:concept-not-disclosed:RIL-FY2025-26-extraordinary-items` | wrong | 22.08 | 40725 | 153 | INR 9.8175 |
