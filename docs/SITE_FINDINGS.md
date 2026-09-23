# How b144.co.il works (reverse-engineered, verified 2026-09-23)

Everything below works over plain HTTP with a Chrome TLS fingerprint (curl_cffi `impersonate="chrome"`).
No login is needed. The site is Next.js: every HTML page embeds its data in
`<script id="__NEXT_DATA__">` → `props.pageProps`. A 200 response *without* `__NEXT_DATA__` means an F5 WAF
challenge (cookies `TS…`).

## Categories

| URL | pageProps key | Notes |
|---|---|---|
| `/indexes/` | `CategoriesTbl` (86) | This is the **"א" letter list**, not the city list |
| `/indexes/{letter}/` (א…ת) | `CategoriesTbl` `[{catCode, catDesc, link:"/X/"}]` | Works. Gives 1,427 unique categories across all 22 letters |
| `/indexes/categories/{letter}/` | `CategoriesTbl` | **Broken**: always returns the "א" list |
| `/indexes/cities/` | `CitiesListsTbl` (115) `[{cityCode, catDesc, link:"/indexes/<city>/"}]` | The city list |
| `/indexes/{city}/` | `CategoriesTbl` `[{catCode, catDesc:"X ב<city>", link:"/X/<city>/"}]` | Up to ~1,361 per big city. Adds 3 categories the letter lists don't have |

The category slug is the first path segment of `link`. The union of all sources gives 1,430 categories.

## Category landing page `/{slug}/`

* `searchObj`: up to ~21 rows. National rows (`AreaName == "ארצי"`, `isArtziService: true`) come first, then a
  preview of each region. `TotalCountAllResults` on any row = the site's total for the category.
* `seoAnalyzerObj`: `{Category, City, CityCode, Category_Code, PageIndex, …}`, exactly the search API payload.
  `Category` uses **spaces** for multi-word categories (`"עורכי דין"`).
* `filtersData.areas`: `[{areaName:"אזור …", membersCount}]` for regions with listings. These counts sum to
  `TotalCountAllResults` minus the national rows, and are often **higher** than the region list's own
  `TotalCount` (see Coverage below).
* The page links to its region pages `/{slug}/אזור-…/`. A region slug = `areaName` with spaces → dashes.

## Regions `/{slug}/אזור-…/`

15 regions, with fixed codes that don't depend on the category:

| Code | Region | Code | Region |
|---|---|---|---|
| −100 | אזור-באר-שבע-והנגב | −108 | אזור-טבריה-וסובב-כינרת |
| −101 | אזור-השרון | −109 | אזור-אילת-והערבה |
| −102 | אזור-ירושלים-והסביבה | −110 | אזור-אשדוד-והסביבה |
| −103 | אזור-תל-אביב-והמרכז | −111 | אזור-גליל-תחתון |
| −104 | אזור-הגליל-העליון-ורמת-הגולן | −112 | אזור-ים-המלח |
| −105 | אזור-גליל-מערבי | −113 | אזור-ראש-העין-הסביבה-והשומרון |
| −106 | אזור-חיפה-הקריות-והסביבה | −114 | אזור-מודיעין-והשפלה |
| −107 | אזור-אשקלון-הנגב-המערבי-והסביבה | | |

Region page `pageProps`:
* `seoAnalyzerObj.CityCode` is the region code.
* `searchObj` is page 1 (15 rows). Every row's `TotalCount` is the region-wide count.
* `citiesCodes` lists the region's city codes.
* `filtersData.cities` holds up to ~20 city names.

A region's list includes national providers and businesses from neighbouring cities that serve the region.

## Search API (all pages after page 1)

```
POST https://services.b144.co.il/Services/b144SearchService.asmx/getSearch
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Authorization: Bearer <URL-decoded ApiToken cookie>
body: jsDetails=<JSON>
```

`jsDetails` = `{"isCardOpen":true,"OriginalDomain":"www.b144.co.il","IsMobile":false,"Category":"<seo.Category>",
"City":"<region or city slug>","CityCode":"<code>","Category_Code":"<catCode>","RewritePath":"","IsRedirect":false,
"Cx":"","Cy":"","IsCoupon":false,"IsOpen":false,"Filter":"","Distance":"","PageIndex":<1-based>}`

* The `ApiToken` cookie is set by any normal page GET (along with the `TS*` F5 cookies).
* The response is XML-wrapped JSON: take the text of `<string>…</string>`, `html.unescape` it, then `json.loads` →
  `{Success, ErrorMessage, Response:[rows]}`. A bad or missing token gives `Success:false, "Access Denied"`.
* 15 rows per page. Page 1 = the server-rendered page. Past the end, `Response` is empty.
* Pagination is stable: two full passes over a region returned identical results with no duplicates.
* **`City` must be a real slug that matches the code.** A made-up slug is rejected. With `City:""` the API
  ignores `CityCode` and returns a capped national preview (26 of 40 for bowling), which is useless for
  full coverage.
* Speed: ~1.2 pages/s at 2 concurrent requests with a 0.3–0.8 s delay.

Row fields include: `Name, Phone` (usually masked, e.g. `"...09-7434"`), `Area_Code, City, Street, Street_No,
Web, FAddress` (Facebook), `InstagramAddress, Service_Location, Rating_avg, Rating_count, Description,
MapX/MapY, MID, Member_ID, catCode, AreaName, isArtziService, isbyCity, TotalCount, TotalCountAllResults`.

## City pages `/{slug}/{city}/` and the coverage gap

Rows come in groups, and each row's `TotalCount` is the size of *its group*, not of the page:

1. **"Serves this city"** (`isbyCity` null, `isArtziService` false): businesses that list this city in their
   service area.
2. National providers.
3. **Businesses located in the city** (`isbyCity: 1`).
4. A national tail.

`TotalCountAllResults` = the whole list for that city.

Some businesses in group 1 appear in **no regional list** at all. Example: a Lod lawyer (MID
`481404134470655D4C1003164175675D40`) whose service areas are Bat Yam, Holon, Lod, Rishon and Ramla. The
located-in-city group (3) was always fully covered by the regional lists. That's why the city sweep reads only
group 1.

The difference between the landing page's `filtersData.areas` counts and the region lists is mostly counting
semantics, not missing businesses:

| Category | Site total | Σ regions | Real extra businesses found in city lists |
|---|---|---|---|
| חשמלאים | 1,915 | 1,915 | n/a (exact match) |
| באולינג | 40 | 40 | n/a |
| אבחון דידקטי | 127 | 127 | n/a |
| עורכי דין | 11,690 | 11,583 | 5 (prototype sweep over 14 gap regions, 372 requests) |
| שגרירות | 323 | 287 | 0 (144 cities swept; Tel Aviv's full list of 129 already covered) |

Row counts are lower than site totals because a business serving several regions appears in each region but
is stored once per category (electricians: 1,915 regional appearances → 1,755 unique businesses).

## Business page `/b144_sip/{MID}/`

`pageProps.initialMemberPageData` (no `__NEXT_DATA__` → WAF; missing or `is404` → business gone):

| Field | Meaning |
|---|---|
| `Phone` | Main number. 076-… = B144 call-tracking (virtual). For free listings it's often empty, and the number is then `Area_Code` + `phone` (e.g. `02` + `5714936`) |
| `MobilePhone`, `MorePhoneNumbers[]` | The business's own mobile and other numbers (the "טלפונים נוספים" button on the site) |
| `Email`, `Fax`, `WebSite`, `FAddress`, `InstagramAddress`, `TikTokAddress`, `YouTubeAddress`, `WoltAddress`, `GmbUrl`, `links[{address, desc}]` | Contact and social links |
| `Address`, `Street`, `Street_No`, `City`, `Zip_code` (`"0"` = none), `MapX` (lon), `MapY` (lat) | Location |
| `MemberOpenHours{html, schema}`, `RemarksOpenHours` | Hours. The HTML holds one `<div class="day">` per day |
| `AreasServices[]`, `Categories[{name, link}]`, `SubCategories[]`, `Additionals[]`, `memberLanguages[]` | Service areas and tags |
| `IsWhatsapp`, `Rating_avg`, `Rating_count`, `Description`, `Member_ID`, `EncryptedMemberId` (= MID) | Other details |

These were checked against the live page in a browser for one business: main phone, mobile, other phone, zip,
all 7 days of hours and languages matched.

Speed: ~1 page/s at the defaults.
