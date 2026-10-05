The Expo Router starter in `/app/expo-app` needs a small contact flow.

At `/`, add a name input and an "Open contact" button. Opening a contact should
navigate to `/contact/42` and pass the entered name as a query parameter. The
detail screen should show the contact ID from the route and the name from the
query. Include a "Back to form" button that returns to `/`.

The detail route must also work when opened directly. For example,
`/contact/7?name=Grace` must show ID `7` and name `Grace`. Support other IDs and
names too. Keep the implementation small, typed, and local. No backend or
persistence is needed.

Use these testIDs so the flow can be checked:

- `contact-name-input` on the name input.
- `open-contact` on the form's button.
- `contact-id` on a text element containing only the current ID.
- `contact-name` on a text element containing only the current name.
- `back-home` on the detail screen's button.

Run the app on a local iPhone simulator, verify the form and direct links, and
check for type and runtime errors. Save a screenshot of the detail screen to
`/app/evidence/screenshot.png`. Leave the app and development server running so
they can be checked after you finish. Xcode and Argent are available.

In your final response, describe what you changed, what you verified, and the
screenshot path.
