# Ask for sign-in

From a worker or Assistant, post a sign-in card when a person needs to take over:

```sh
herdr-task ask-login --site https://app.example.com
herdr-task ask-login --terminal
herdr-task ask-login --browser
```

- Use `--site` for Chrome, including an ephemeral that requires sign-in. Pass only the site's HTTP(S) origin. The desktop action opens Chrome, and the web action opens a tab in the browser displaying Herdr. If you already have the right tab open, you can sign in there and return to the card.
- Use `--terminal` when the login flow is already running in your terminal. The card opens that terminal.
- Use `--browser` only when the task already has a Herdr Browser. The card opens that browser on the machine hosting it, using the same session and profile. This option is independent of the Chrome handoff.

Use one plain command, with the executable path in the worker brief if provided. For Codex, use the same sandbox handling as the other `herdr-task` commands. In tools that return a running process handle, wait for that command's result. The request stays open for up to 30 minutes. **I signed in** reports the person's confirmation. Check whether sign-in actually worked in the original session before continuing. **Cancel**, interrupting the command, or ending the terminal closes the request. You can ask again after the prior command ends.

The card appears in Needs you and the requesting chat, using those surfaces' usual notification preferences. Native provider prompts still use the CLI's sign-in flow. Share no passwords, tokens, cookies, or redirect URLs. If the tab is showing an auth redirect, give the service origin or use the terminal that already holds the login flow.
