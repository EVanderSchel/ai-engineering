# Blind grading: baseline judged answers

For each answer, decide **pass** or **fail** against its criteria, the same task the judge had. Write your grade after `Your grade:` (and a few words why, if it's a close call). Don't open results.jsonl until you're done: it holds the judge's verdicts.

Figures are already checked by code; grade only the criteria.

## 1. What was the Altoona, Iowa Habitat for Humanity affiliate's revenue in 2022?

*Case q16, run 1*

**Criteria:** Says there is no 2022 financial data for this organization. Gives no number as 2022 revenue. Mentioning other years, clearly labeled with their year, is fine.

**Tool calls:** search_organizations(query=Habitat for Humanity, state=IA); get_organization(ein=42-1520979); get_organization(ein=58-1235159); get_organization(ein=42-1484702); get_financials(ein=42-1520979, tax_year=2022) -> ERROR

**Answer:**

> I identified the active Altoona, Iowa Habitat for Humanity affiliate as EIN 42-1520979 (the one with the most recent/ongoing filings, through 2023). However, **tax year 2022 has no financial data available** for this organization — its filing years jump from 2021 directly to 2023, meaning 2022 was either not filed or filed without figures captured in this data source.
> 
> For reference, here's what is available around that year:
> - **2021**: data available
> - **2022**: no data (gap in filings)
> - **2023**: data available
> 
> Would you like me to pull the 2021 or 2023 revenue figures instead? Also note: two other Altoona-based Habitat entries (EIN 58-1235159 and 42-1484702) appear to have stopped filing after 2020–2021, suggesting they may be predecessor/merged entities rather than the current affiliate — let me know if you'd like me to check those as well.

**Your grade:** 

Pass

## 2. Did the Cedar Valley Humane Society's expenses grow between 2021 and 2023? By how much?

*Case q06, run 0*

**Criteria:** Says expenses did not grow: they decreased from 2021 to 2023.

**Tool calls:** search_organizations(query=Cedar Valley Humane Society); compute_ratios(ein=42-0814023, tax_year=2023, compare_with_year=2021)

**Answer:**

> Cedar Valley Humane Society's (EIN 42-0814023, Cedar Rapids, IA) expenses did **not** grow — they actually decreased slightly between tax year 2021 and tax year 2023, based on Form 990 filings.
> 
> - Change in total expenses: **-$34,955 (a decline of about 3.6%)**
> 
> If you'd like the exact dollar figures for each year, I can pull those from the individual filings.

**Your grade:** 

Pass

## 3. Compare officer pay as a share of expenses at Iowa Public Radio and the Dubuque Humane Society in 2023.

*Case q12, run 2*

**Criteria:** Says the Dubuque Humane Society's share was (slightly) higher than Iowa Public Radio's.

**Tool calls:** search_organizations(query=Iowa Public Radio, state=IA); search_organizations(query=Dubuque Humane Society, state=IA); compute_ratios(ein=20-4227123, tax_year=2023); compute_ratios(ein=42-6039535, tax_year=2023)

**Answer:**

> In tax year 2023 (Form 990), officer compensation made up a very similar share of total expenses at both organizations:
> 
> - **Iowa Public Radio Inc** (EIN 20-4227123, Des Moines, IA): **3.73%** of expenses
> - **Dubuque Humane Society** (EIN 42-6039535, Dubuque, IA): **3.86%** of expenses
> 
> The difference is slight (about 0.13 percentage points), with Dubuque Humane Society spending marginally more of its budget on officer pay relative to total expenses, despite being the much smaller organization of the two.

**Your grade:** 

Pass

## 4. Which of Iowa's largest food banks depends most on donations?

*Case q14, run 2*

**Criteria:** Names Northeast Iowa Food Bank as the most dependent on donations (contributions about 97.3% of revenue), with River Bend Food Reservoir among the food banks it compared. Does not assert that its set of food banks is definitely complete, or that other food banks are much smaller, beyond what it checked.

**Tool calls:** search_organizations(query=food bank, state=IA); search_organizations(query=food reservoir, state=IA); search_organizations(query=foodbank, state=IA); get_financials(ein=42-1177880); get_financials(ein=42-1169648); get_financials(ein=36-3147342); get_financials(ein=20-0076420); get_financials(ein=42-1381516); compute_ratios(ein=42-1177880, tax_year=2023); compute_ratios(ein=36-3147342, tax_year=2023); compute_ratios(ein=42-1169648, tax_year=2023)

**Answer:**

> Among Iowa's three largest food banks by revenue (FY2023, Form 990), **Northeast Iowa Food Bank** (Waterloo, EIN 42-1169648) depends most on donations:
> 
> | Food bank | FY2023 total revenue | Contributions & grants | Contributions as % of revenue |
> |---|---|---|---|
> | **Northeast Iowa Food Bank** (Waterloo) | $21.0M | $20.4M | **97.3%** |
> | Food Bank of Iowa (Des Moines) | $47.2M | $45.3M | 96.0% |
> | River Bend Food Reservoir (Davenport) | $39.1M | $36.4M | 92.9% |
> 
> All three are overwhelmingly donation-funded (90%+), but Northeast Iowa Food Bank has the highest share and, notably, a razor-thin FY2023 surplus margin (0.4%), making it the most donation-dependent of the group.
> 
> Note on scope: I searched "food bank," "foodbank," and "food reservoir" within Iowa and sized candidates by total revenue. Food Bank of Siouxland ($6.8M revenue, 95.3% contributions) and Linn Community Food Bank (only ~$200K revenue — a much smaller, separate organization despite the name) are far smaller and not among the "largest." I did not exhaustively check every small county-level "food bank" entity turned up by the search (e.g., Brooklyn, Delaware County, Iowa County), since those are clearly local-scale rather than among Iowa's largest.

**Your grade:** 

Pass

## 5. Which had more revenue in 2023: the Dubuque Humane Society or the Cedar Valley Humane Society?

*Case q11, run 2*

**Criteria:** Says the Cedar Valley Humane Society had more revenue in 2023.

**Tool calls:** search_organizations(query=Dubuque Humane Society, state=IA); search_organizations(query=Cedar Valley Humane Society, state=IA); get_financials(ein=42-6039535, tax_year=2023); get_financials(ein=42-0814023, tax_year=2023)

**Answer:**

> For tax year 2023 (Form 990, fiscal year ended December 2023):
> 
> - **Cedar Valley Humane Society** (Cedar Rapids, IA, EIN 42-0814023): **$2,324,147** total revenue — more revenue
> - **Dubuque Humane Society** (Dubuque, IA, EIN 42-6039535): $1,457,953 total revenue
> 
> Cedar Valley Humane Society had the higher revenue, by about $866,194.

**Your grade:** 

Pass

## 6. What was Iowa Public Radio's revenue in 2024?

*Case q15, run 2*

**Criteria:** Says no 2024 revenue figure is available (the 2024 return was filed, but only as a PDF without extracted figures). Gives no number as 2024 revenue. Offering figures for an earlier year, clearly labeled with that year, is fine.

**Tool calls:** search_organizations(query=Iowa Public Radio); get_financials(ein=20-4227123, tax_year=2024) -> ERROR

**Answer:**

> Iowa Public Radio Inc (EIN 20-4227123) filed its tax year 2024 Form 990, but only as a scanned PDF — the IRS data extract doesn't include parsed financial figures for that year, so I can't report its 2024 revenue.
> 
> The most recent year with usable figures is tax year 2023. Would you like that instead?

**Your grade:** 

Pass

## 7. How did the Altoona Habitat for Humanity's revenue change in 2023 compared with the year before?

*Case q21, run 0*

**Criteria:** Says 2022 (the year before 2023) has no financial data. Any comparison it gives is clearly labeled as against 2021 (or another named year), not presented as the year before. Gives no figure as 2022 revenue.

**Tool calls:** search_organizations(query=Altoona Habitat for Humanity); search_organizations(query=Habitat for Humanity Altoona); search_organizations(query=Habitat for Humanity, state=IA); get_organization(ein=42-1520979); get_organization(ein=58-1235159); get_organization(ein=42-1484702); get_financials(ein=42-1520979, tax_year=2023); compute_ratios(ein=42-1520979, tax_year=2023)

**Answer:**

> I used **Habitat for Humanity International Inc. (Altoona, IA)**, EIN 42-1520979 — the Altoona-based affiliate that has 2023 filing data (its fiscal year ends in June, so "2023" = FY ended 6/30/2023).
> 
> **Important caveat:** This organization has no financial data on file for tax year 2022 (it's missing from both the "filed with data" and "filed without data" lists, so it appears not to have been filed/posted). The most recent prior year with usable figures is **2021**, so the comparison below is 2023 vs. 2021, not 2023 vs. 2022.
> 
> **Revenue change, FY2023 vs. FY2021 (Form 990):**
> - FY2023 total revenue: $2,480,061
> - FY2021 total revenue: $1,710,261 (implied: $2,480,061 − $769,800)
> - Change: **+$769,800, a 45.0% increase**
> 
> This was driven mostly by contributions and grants, which made up 89% of FY2023 revenue ($2,210,634). Expenses actually fell slightly over the same span (−6.9%), so the surplus grew substantially (FY2023 surplus: $1,140,132, a 46% margin).
> 
> If you need the true year-over-year (2022) figure, it isn't available in the IRS data served here — only FY2021, FY2023, and earlier years back to FY2011 are posted with figures.

**Your grade:** 

Pass

## 8. What was Habitat for Humanity's revenue in Iowa in 2023?

*Case q20, run 2*

**Criteria:** Recognizes that Habitat for Humanity in Iowa is many separate affiliates (several organizations match) and either asks which one, reports clearly identified individual affiliates, or explains why it can't give one statewide figure. Does not present a single affiliate's revenue as Habitat for Humanity's revenue in Iowa.

**Tool calls:** search_organizations(query=habitat for humanity, state=IA); search_organizations(query=habitat for humanity, state=IA, page=1)

**Answer:**

> There is no single statewide "Habitat for Humanity – Iowa" entity. Instead, each local affiliate (Des Moines, Cedar Rapids, Waterloo, Dubuque, Sioux City, Iowa City, etc.) files its own IRS return — I found 27 separate Habitat for Humanity affiliates registered in Iowa. To answer "revenue in Iowa in 2023" I'd need to pull and sum 2023 figures for each of these 27 filers, which is a lot of calls.
> 
> Could you clarify whether you want:
> 1. A specific local affiliate (e.g., "Habitat for Humanity of Greater Des Moines"), or
> 2. A combined total across all Iowa affiliates (I can do this, but it will take several tool calls and some affiliates may not yet have 2023 figures filed)?
> 
> Let me know which, and I'll pull the exact figures with tax year and form noted.

**Your grade:** 

Pass

## 9. What was the Hawkeye Riverside Charitable Trust's revenue?

*Case q19, run 0*

**Criteria:** Says no organization by that name was found. Does not give another organization's revenue as if it were this organization's.

**Tool calls:** search_organizations(query=Hawkeye Riverside Charitable Trust); search_organizations(query=Hawkeye Riverside); search_organizations(query=Hawkeye Charitable Trust); search_organizations(query=Hawkeye); search_organizations(query=Riverside Charitable Trust)

**Answer:**

> I couldn't find an organization named "Hawkeye Riverside Charitable Trust" in the ProPublica Nonprofit Explorer database. I searched for "Hawkeye Riverside Charitable Trust," "Hawkeye Riverside," "Hawkeye Charitable Trust," and "Riverside Charitable Trust" — all returned zero matches. A broader search for just "Hawkeye" returned 307 results (shown as the top 25, none matching "Riverside Charitable Trust").
> 
> This could mean the organization is not registered as a tax-exempt entity under this name, the name is spelled differently in IRS records, or it may not be required to file with the IRS (e.g., certain trusts). Could you confirm the exact legal name, or provide an EIN or state where it's based, so I can search further?

**Your grade:** 

Pass

## 10. How has The Beacon in Des Moines' revenue changed since 2020?

*Case q22, run 2*

**Criteria:** States which organization record it used: Beacon Of Life (EIN 42-1253088, Des Moines). Describes revenue falling in 2021 and 2022 before rising in 2023, not as steady growth.

**Tool calls:** search_organizations(query=The Beacon, state=IA); search_organizations(query=Beacon, state=IA); get_organization(ein=42-1253088); get_financials(ein=42-1253088); compute_ratios(ein=42-1253088, tax_year=2023, compare_with_year=2020)

**Answer:**

> ## Beacon Of Life — Revenue Change, FY2020 to FY2023
> 
> Total revenue rose **17.6%**, from **$502,347** (tax year 2020, fiscal year ended June 2020, Form 990) to **$590,770** (tax year 2023, fiscal year ended June 2023, Form 990) — an increase of **$88,423**.
> 
> The path wasn't steady, though:
> | Tax Year (FYE) | Total Revenue |
> |---|---|
> | 2020 (6/2020) | $502,347 |
> | 2021 (6/2021) | $448,232 |
> | 2022 (6/2022) | $405,611 |
> | 2023 (6/2023) | $590,770 |
> 
> Revenue actually dipped in 2021 and 2022 (down to $405,611 — a 19% drop from 2020) before rebounding sharply in 2023 to its highest level in the period, driven mainly by a jump in contributions and grants ($161,968 in 2022 to $331,236 in 2023).
> 
> Note: the organization's most recent filing on record is for tax year 2024, but it was filed without financial figures, so FY2023 is the latest year with reported revenue data.

**Your grade:** 

Pass

## 11. What percentage of Food Bank of Iowa's expenses went to programs in 2023?

*Case q17, run 2*

**Criteria:** Says the available data does not include program expenses, so the share of expenses spent on programs can't be determined. Gives no percentage as the program share. Other figures, clearly labeled as something else, are fine.

**Tool calls:** search_organizations(query=Food Bank of Iowa, state=IA); get_financials(ein=42-1177880, tax_year=2023)

**Answer:**

> The available data for Food Bank of Iowa (EIN 42-1177880, Des Moines, IA) for tax year 2023 (fiscal year ended June 2023, Form 990) does not include a breakdown of expenses by function (e.g., program services vs. management/fundraising) — only total expenses ($40,416,626), along with revenue, assets, liabilities, compensation and fundraising fees.
> 
> Since the IRS data I can access doesn't report a separate "program service expenses" line item for this filing, I can't calculate the percentage of expenses that went to programs. If you'd like, I can share the figures that are available (total revenue, expenses, officer compensation, etc.) or check a different tax year to see if that breakdown was reported.

**Your grade:** 

Pass

## 12. How has The Beacon in Des Moines' revenue changed since 2020?

*Case q22, run 1*

**Criteria:** States which organization record it used: Beacon Of Life (EIN 42-1253088, Des Moines). Describes revenue falling in 2021 and 2022 before rising in 2023, not as steady growth.

**Tool calls:** search_organizations(query=The Beacon, state=IA); search_organizations(query=Beacon, state=IA); get_organization(ein=42-1253088); get_financials(ein=42-1253088); compute_ratios(ein=42-1253088, tax_year=2023, compare_with_year=2020)

**Answer:**

> The organization I found is **Beacon of Life** in Des Moines, IA (EIN 42-1253088) — Des Moines doesn't have a separate exact-name match for "The Beacon," so I'm using this one; let me know if you meant a different organization.
> 
> **Revenue trend since FY2020 (Form 990, fiscal years ending June 30):**
> 
> | Tax year | Total revenue |
> |---|---|
> | 2020 | $502,347 |
> | 2021 | $448,232 |
> | 2022 | $405,611 |
> | 2023 | $590,770 |
> 
> - From FY2020 to FY2023, total revenue rose **$88,423, or about +17.6%**.
> - The path wasn't steady: revenue actually declined in FY2021 and FY2022 (down to $405,611, the low point) before rebounding sharply to $590,770 in FY2023, largely driven by a jump in contributions and grants (from $161,968 in 2022 to $331,236 in 2023).
> - Despite the 2023 revenue recovery, expenses also grew (+17.8% from 2020), and the org ran a deficit of about $87,214 in FY2023 (margin ‑14.8%).
> - Note: FY2024 was filed but without financial figures available yet in this data.

**Your grade:** 

Pass

## 13. What are the two largest food banks in Iowa by 2023 revenue?

*Case q13, run 0*

**Criteria:** Names Food Bank of Iowa (Des Moines) as the largest and River Bend Food Reservoir (Davenport) as the second largest by 2023 revenue.

**Tool calls:** search_organizations(query=food bank, state=IA); search_organizations(query=foodbank, state=IA); search_organizations(query=food reservoir, state=IA); get_financials(ein=42-1177880, tax_year=2023); get_financials(ein=42-1169648, tax_year=2023); get_financials(ein=42-1381516, tax_year=2023); get_financials(ein=20-0076420, tax_year=2023); get_financials(ein=36-3147342, tax_year=2023); get_financials(ein=42-1207197, tax_year=2023) -> ERROR; get_financials(ein=42-1184748, tax_year=2023)

**Answer:**

> Based on searches for "food bank," "foodbank," and "food reservoir" organizations headquartered in Iowa, the two largest by 2023 total revenue are:
> 
> 1. **Food Bank of Iowa** (Des Moines, EIN 42-1177880) — $47,199,579 total revenue (FY2023, ending June 2023, Form 990)
> 2. **River Bend Food Reservoir** (Davenport, EIN 36-3147342) — $39,142,904 total revenue (FY2023, ending June 2023, Form 990)
> 
> For comparison, other Iowa food banks checked for 2023: Northeast Iowa Food Bank (Waterloo) — $20,990,066; Food Bank of Siouxland (Sioux City) — $6,754,062; Hawkeye Harvest Food Bank (Mason City) — $1,442,234; Linn Community Food Bank (Cedar Rapids) — $201,799 (a smaller, separately registered entity — the larger Cedar Rapids-area food bank operates under HACAP, which wasn't captured by these name searches). Food Bank of Southern Iowa (Ottumwa) has no financial data filed since 2017, so it couldn't be compared for 2023.
> 
> This covers only organizations whose names contain "food bank," "foodbank," or "food reservoir" — I can't guarantee this is a complete list of every food-bank-type organization in Iowa (e.g., some may operate as community action agencies without "food bank" in their name).

**Your grade:** 

Pass

## 14. How much did the Sehgal Foundation pay its officers in 2023?

*Case q18, run 2*

**Criteria:** Says officer compensation is not reported in the available data for this foundation (it files Form 990-PF). Does not say it paid $0 and gives no amount as officer pay.

**Tool calls:** search_organizations(query=Sehgal Foundation); get_financials(ein=42-1477858, tax_year=2023); get_financials(ein=84-3912292, tax_year=2023)

**Answer:**

> Both Sehgal Foundation entries I found filed **Form 990-PF** for tax year 2023 (Des Moines, IA, EIN 42-1477858, and Los Altos, CA, EIN 84-3912292). Form 990-PF does not report officer compensation as a separate line item in the data available here — that field only appears on the full Form 990, and both of these organizations' filings show it as null (not zero, just not reported in this dataset).
> 
> So I can't give you an officer-compensation figure for either Sehgal Foundation for 2023 from this data. If you meant a different, larger organization by this name (e.g., one that files a full Form 990), let me know and I can search further.

**Your grade:** 

Pass

## 15. Which of Iowa's largest food banks depends most on donations?

*Case q14, run 1*

**Criteria:** Names Northeast Iowa Food Bank as the most dependent on donations (contributions about 97.3% of revenue), with River Bend Food Reservoir among the food banks it compared. Does not assert that its set of food banks is definitely complete, or that other food banks are much smaller, beyond what it checked.

**Tool calls:** search_organizations(query=food bank, state=IA); search_organizations(query=foodbank, state=IA); search_organizations(query=food reservoir, state=IA); get_financials(ein=42-1177880); get_financials(ein=42-1169648); get_financials(ein=42-1381516); get_financials(ein=20-0076420); get_financials(ein=36-3147342); get_financials(ein=42-1207197); get_financials(ein=42-1184748); compute_ratios(ein=42-1177880, tax_year=2023); compute_ratios(ein=36-3147342, tax_year=2023); compute_ratios(ein=42-1169648, tax_year=2023); compute_ratios(ein=42-1381516, tax_year=2023)

**Answer:**

> ## Northeast Iowa Food Bank (Waterloo, EIN 42-1169648) depends most on donations among Iowa's largest food banks
> 
> Based on FY2023 Form 990 filings for Iowa's four largest food banks (by total revenue), **contributions and grants as a share of total revenue**:
> 
> | Food Bank | Tax Year (FYE) | Total Revenue | Contributions Share of Revenue |
> |---|---|---|---|
> | **Northeast Iowa Food Bank** (Waterloo) | 2023 (June) | $21.0M | **97.3%** |
> | Food Bank of Siouxland (Sioux City) | 2023 (Sept) | $6.8M | 95.2% |
> | Food Bank of Iowa (Des Moines) | 2023 (June) | $47.2M | 96.0% |
> | River Bend Food Reservoir (Davenport) | 2023 (June) | $39.1M | 92.9% |
> 
> All four of Iowa's largest food banks are overwhelmingly donation-dependent (92–97% of revenue), as is typical for the sector, but **Northeast Iowa Food Bank** has the highest reliance at 97.3%, with program service revenue (e.g., fees) making up only a sliver of its budget.
> 
> **Scope note:** I searched for "food bank," "foodbank," and "food reservoir" organizations registered in Iowa. This identified 17 matches plus River Bend Food Reservoir. I focused on the ones with the largest total revenue — Food Bank of Iowa, River Bend Food Reservoir, Northeast Iowa Food Bank, and Food Bank of Siouxland — since the others (e.g., Linn Community Food Bank, Hawkeye Harvest, Food Bank of Southern Iowa) report far smaller budgets ($200K–$1.4M, and Food Bank of Southern Iowa's last available filing is from 2017). I can't rule out a larger or more donation-dependent food bank operating under a name that doesn't contain these search terms (e.g., "food pantry" or a county-specific name).

**Your grade:** 

Pass
