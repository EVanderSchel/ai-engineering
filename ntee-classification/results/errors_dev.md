## Agreement across the five approaches (dev)

| | Share of missions |
|---|---|
| 5 of 5 right | 39.0% |
| 4 of 5 right | 11.6% |
| 3 of 5 right | 9.7% |
| 2 of 5 right | 8.2% |
| 1 of 5 right | 6.9% |
| 0 of 5 right | 24.6% |

## Group pairs every approach confuses (either direction)

| Pair | Groups | TF-IDF | Haiku | Sonnet | LoRA 0.5B | LoRA 1.5B |
|---|---|---|---|---|---|---|
| A-B | Arts, Culture and Humanities / Education | 22 | 22 | 18 | 25 | 27 |
| B-O | Education / Youth Development | 19 | 23 | 18 | 19 | 25 |
| N-O | Recreation and Sports / Youth Development | 24 | 21 | 18 | 17 | 19 |
| P-T | Human Services / Philanthropy, Voluntarism and Grantmaking Foundations | 24 | 16 | 16 | 22 | 18 |
| P-X | Human Services / Religion-Related | 25 | 14 | 22 | 36 | 26 |
| B-T | Education / Philanthropy, Voluntarism and Grantmaking Foundations | 27 | 26 | 19 | 14 | 18 |
| B-P | Education / Human Services | 13 | 19 | 28 | 33 | 32 |
| E-G | Health Care / Diseases, Disorders and Medical Disciplines | 17 | 19 | 13 | 14 | 14 |
| O-P | Youth Development / Human Services | 23 | 29 | 19 | 20 | 12 |
| L-P | Housing and Shelter / Human Services | 16 | 23 | 14 | 17 | 12 |
| L-S | Housing and Shelter / Community Improvement and Capacity Building | 13 | 12 | 13 | 14 | 13 |
| P-S | Human Services / Community Improvement and Capacity Building | 11 | 17 | 22 | 23 | 23 |

## Accuracy by mission length (words)

| Words | Missions | TF-IDF | Haiku | Sonnet | LoRA 0.5B | LoRA 1.5B |
|---|---|---|---|---|---|---|
| 1-5 | 188 | 57% | 58% | 59% | 57% | 60% |
| 6-10 | 314 | 51% | 55% | 58% | 55% | 56% |
| 11-20 | 447 | 53% | 57% | 63% | 58% | 62% |
| 21-40 | 580 | 54% | 60% | 63% | 62% | 62% |
| 41+ | 284 | 55% | 63% | 68% | 59% | 59% |

## The cascade's routing (threshold 0.7)

- Sent to Sonnet: 53% of missions (median 20 words, vs 19 for those kept).
- On the missions sent, Sonnet is right 47% of the time, the small model 39%; on those kept, the small model is right 81%.
- Groups sent most: U 93%, R 89%, T 85%, G 85%, V 83%, Y 81%
- Groups sent least: N 32%, A 31%, M 28%, D 13%

## The shortest missions all five got wrong

| Mission | IRS | TF-IDF | Haiku | Sonnet | LoRA 0.5B | LoRA 1.5B |
|---|---|---|---|---|---|---|
| DOWNTOWN REVITALIZATION. | R | S | S | S | S | S |
| Educating students. | P | B | B | B | B | B |
| FUNDS MANAGMENT | N | W | T | Y | S | S |
| EDUCATIONAL DEMONSTRATIONS | P | B | B | B | A | B |
| HISTORIC PRESERVATION | C | A | A | A | A | A |
| PUBLIC ASSISTANCE | O | M | P | P | T | P |
| TREATMENT OF ALCOHOLISM | A | F | F | F | F | F |
| RECREATIONAL SOCCER PROGRAM | O | N | N | N | N | N |
| AVIATION TRADE ASSOCIATION | Y | S | S | S | S | S |
| Eliminate community violence | P | I | I | I | I | I |

## Label check of hard vs control missions (graded blind)

| Missions | Graded | Agree with IRS | Unsure | Disagree |
|---|---|---|---|---|
| hard | 25 | 20% | 8% | 72% |
| control | 15 | 80% | 20% | 0% |

On the 18 hard missions where you disagreed with the IRS, each approach chose the group you picked: TF-IDF 11, Haiku 13, Sonnet 16, LoRA 0.5B 11, LoRA 1.5B 14
