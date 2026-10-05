The notes app in `/app/expo-app` can add and delete notes, but loses them when
the app reloads. Make the notes persist locally using `expo-sqlite`. Install the
SDK-compatible package and keep the existing add/delete behavior and TypeScript
checks. Do not replace the app with a demo or hardcode example notes.

Keep the notes screen at `/`. Preserve these testIDs so it can be checked:
`note-input` for the controlled text input, `add-note` for its button,
`note-list` for the list, `note-<id>` on each note's text, and
`delete-note-<id>` on that note's delete button. IDs must identify individual
notes. Saving should clear the input; empty notes should not be saved.

Run it on a local iPhone simulator. Demonstrate adding two different notes,
reloading the app, and deleting one note without losing the other. Confirm that
the deletion also survives a reload. Check types and runtime errors. Xcode and
Argent are available; Expo Go or a development build is fine.

Save a screenshot of the working notes screen to `/app/evidence/screenshot.png`.
Leave the app and development server running. In your final response, summarize
what you changed, what you verified, and any remaining problems.
