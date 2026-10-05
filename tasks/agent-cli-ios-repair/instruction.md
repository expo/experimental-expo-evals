The prepared Expo project in `/app/expo-app` is a small coffee cart. It crashes
when opened, and its total calculation is wrong. Repair both problems and make
sure the project passes its TypeScript checks.

Keep the two products and their prices unchanged: Coffee costs $7.50 and Tea
costs $3.00. The cart starts with two coffees and one tea ($18.00). Adding one
coffee must give $25.50; then adding one tea must give $28.50. Keep the initial
quantities, the add buttons, and these testIDs: `cart-title`, `cart-total`,
`quantity-coffee`, `quantity-tea`, `add-coffee`, and `add-tea`. Quantity labels
contain just the number; the total label contains the dollar amount. Preserve
`src/products.json` and `tsconfig.json`. Fix the source rather than disabling
checks, suppressing errors, or hard-coding the example totals.

Run the repaired app on a local iPhone simulator on this Mac. Verify both add
buttons and save a PNG screenshot to `/app/evidence/screenshot.png`. Leave the
app and development server running so they can be checked after you finish.
Xcode and Argent are available.

In your final response, describe the bugs, what you changed, what you verified,
and the screenshot path.
