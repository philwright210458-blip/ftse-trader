# FTSE Trader: model review and next steps

*Review date: 17 October 2026*

The machinery around the model is in good shape: it runs on its own, records everything and measures results honestly. There is no evidence yet that the model makes money, and 1% a day is not a realistic target. The next two to three months are a test the model has to pass.

## Where the model stands today

One day of usable results so far, and it is negative after costs.

| Measure | Result |
| --- | --- |
| Days of usable results | 1 (Thursday 15 October) |
| Ideas tested (held up to one day) | 71 |
| Target hit / stop hit / closed at end of day | 9 / 47 / 15 |
| Average result per £100 trade, after all fees | about 50p loss |
| Total across 71 paper trades of £100 | £34.72 loss |

One day proves nothing either way. The 71 trades all happened on the same day, so they behave more like one trade than 71. A fair verdict needs at least 100 to 200 trades spread over 40 to 60 trading days, which is about two to three months.

## Pros

- **Results are measured honestly.** Mock trades only use prices you could actually have got. A missing price never counts as a win, and stamp duty and dealing costs are included.
- **It runs without you.** The scans, re-checks and scoring run on GitHub every weekday, started by cron-job.org, with no laptop needed.
- **It builds a proper track record.** Every idea is logged, so you can see which kinds of setups work: by score, setup type, buy or sell, FTSE 100 or 250.
- **You're testing on paper first.** No real money is at risk until there's evidence.
- **It's ready to compare improvements.** The workbook shows whether the top picks beat the average idea, so changes can be tested one at a time.

## Cons and risks

1. **Costs are huge compared with the stops.** The typical stop is about 0.6% below entry, and stamp duty alone is 0.5%. On a £100 purchase of real shares, a stopped-out trade loses about 64p, costs add about 60p, and a winner makes about £1.90 before costs. You'd need to hit the target about half the time to break even; so far it's about 1 time in 8.
2. **Most ideas are "sell" trades you can't take.** On Thursday 71 of the 125 ideas were sells, which a normal share account can't do.
3. **The price data is free and limited.** Yahoo gives 15-minute candles, not live prices, so paper results will always be somewhat better than real ones.
4. **The software is growing faster than the evidence.** About 35,000 lines of code, a website and replays, but only one day of results. The next step is evidence, not more features.
5. **It's tempting to tinker.** Changing rules after every bad patch finds settings that fit the past and fail in future. Change one thing at a time, and only after enough results.

## The 1% a day target, honestly

Compounded, these targets are far beyond what professional investors achieve.

| Target | Over a year |
| --- | --- |
| 1% a day (about 252 trading days) | each £1 becomes about £12.30 (a 1,130% gain) |
| 4% a week (52 weeks) | each £1 becomes about £7.70 (a 670% gain) |
| UK shares over the long run | roughly 5 to 8% a year |

The best professional funds in history have averaged tens of percent a year, not hundreds. IG states that 70% of its retail clients lose money on spread bets and CFDs.

The target also clashes with this model. A winning trade makes about 1.9% on the money in it. To make £100 (1%) on a £10,000 account in a day, you'd need over £5,000 in trades hitting their targets that day with no losers, which means putting most of the account at risk daily.

**A more useful target:** first prove the model makes anything at all after costs over two to three months. Then judge it by yearly return and worst losing stretch. 10 to 20% a year with small losing streaks would be a genuine success.

## When to increase from £100

Decide the test before seeing the results. These match the starting rules already in `config/research.json`:

- [ ] At least 60 trading days of results
- [ ] At least 100 completed trades
- [ ] A profit after stamp duty and dealing costs
- [ ] Worst losing stretch no bigger than 10% of the account
- [ ] Top picks clearly beating the average idea

If it passes, increase in steps (for example £100, then £250, then £500) and check each step still holds up with real fills.

## Automating real trades later

Automate only after the model passes the test above; automating a losing model loses money faster.

1. **Prove the model first.**
2. **Choose the account type**, because stamp duty depends on it.
    - Buying real shares (Trading 212, Interactive Brokers and others): stamp duty applies, and you can't sell short.
    - Spread betting (IG and others): no stamp duty, you can bet on falls, and UK spread-betting profits are currently tax-free. It uses borrowed money, so losses can exceed your deposit, and most retail clients lose.
3. **Choose a broker with an API** (a way for code to place trades).
    - [Trading 212's API](https://docs.trading212.com/api/orders) documents market, limit, stop and stop-limit orders for shares and ETFs; which account types it covers needs confirming.
    - [IG's API](https://ig.com/uk/apis) supports automated spread betting and CFDs, with a demo account.
    - [Interactive Brokers](https://www.interactivebrokers.co.uk/en/accounts/fees/LSEstkfees.php) has a powerful API covering London shares but is more complex to set up.
4. **Run it on a demo account** for several weeks before real money.
5. **Build in safety limits:** a daily loss limit, maximum trades, maximum size per trade, an off switch and phone alerts.

Not financial advice: check account types and tax treatment with the provider or a professional before committing money.

## Next steps

- [ ] Let it run until mid-December (about 40 trading days) without changing the rules
- [ ] Check the Summary sheet each Friday: profit after all fees, and whether top picks beat the pool
- [ ] In about two weeks, test wider stops (for example 2% instead of 0.6%) side by side, so costs take a smaller share of each move
- [ ] Leave automation until the model passes the scaling test

## Sources

- [Trading 212 API: orders](https://docs.trading212.com/api/orders)
- [IG: trading APIs](https://ig.com/uk/apis)
- [Interactive Brokers: London Stock Exchange fees](https://www.interactivebrokers.co.uk/en/accounts/fees/LSEstkfees.php)
