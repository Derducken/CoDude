# CoDude
The geek's answer to CoPilot

Vibe-coded by multiple LLMs based on OK's instructions (plus some manual tweaking).

## What's that?!
Ever heard of Microsoft's CoPilot? Awesome! This ain't it. CoDude's goal is to run as a resident app, lingering on your system's tray, waiting for its textual prey. When you use a key combination to command CoDude to attack his target, he'll launch from the shadows, grab it from the Clipboard, and offer you various options on how to deal with it. Will you have CoDude...

- Explain your text selection as if you're 2 years old?
- Summarize the selected text in three words, or less?
- Roast it as a disappointed superhero?

It's up to you.

## Commands (and how to craft'em)

The app comes with some sample recipes that you'll see as buttons on its left side.

You can edit all recipes, or replace them with your own, by editing the Recipes.md file with a text editor. You can quickly access this file from the app's CoDude menu. The command structure relies on markdown, and is:

```Markdown
**Command Name**: Instructions to the LLM about what you want to do with the text you selected:
```

Notice that the "Command Name" is in bold (two asterisks before and after it in Markdown syntax), and followed by ":" and a space. The instructions are normal text, and should all be structured as a single line of text - no breaks, no pressing Enter and adding lists. They should also end with ":". The text you selected and copied to the clipboard is appended automatically after the command.

You can group commands by using typical Markdown headings. Every heading is automatically recognized as a group, and the commands underneath it as members of that group. Checking the recipes.md file in combination with "how the default commands show up on the app's interface" will help you make better sense of "how it works".

Each recipe and heading must be followed by an empty line in the markdown file.

Apart from predefined commands, you can also use the Custom Input text field to type a custom command describing "what you want to do to the text you've copied to the Clipboard". You can send that command to the LLM (together with the text you've selected) with a click on Send Custom Command, or by pressing CTRL+Enter.

On the right of all that wonderful stuff you'll be able to see the text you selected in another editable textarea. You can manipulate it there before unleashing a command on it. Cut it down, streamline it, and edit it like a pro, while wondering "why the heck am I doing what I should have the LLM do for me in the first place? Is there any meaning in all of this? Help!".

The results of CoDude's hard work (...as a glorified intermediary between you and the configured LLM...) will pop up on a new window as soon as your LLM decides to grace us with its reply. From this window you can export this reply to a markdown file locally or copy it to the clipboard. There are buttons for that. They're clickable. GUI craftmanship at its finest.

However, if you don't like all those pop up windows (that are also somewhat buggy in the current version), you can choose to have the LLM responses integrated as a third column on CoDude's main window, from the app's Configuration.

Note that you can also switch the Custom Input to Chat Mode, if you want to continue chatting with the LLM about any previous interraction, or talk about the weather. This will auto-enable Append Mode in the LLM Results field, and have that field work like a typical chat between you and the LLM, where each new message is a progression of everything that came before. 

## Configuration

You'll find the app's configuration "hidden" in CoDude's main menu (named "CoDude", obviously, duh!).

From there, you can...

- Define the LLM's URL (OpenAI-friendly, like the "http://127.0.0.1:7777" I'm using in LM Studio, or "something like that").
- Choose the hotkey you want to use to "call" the app. By default, it's **CTRL** + **Alt** + **C**.
- Switch between normal and dark themes.
- Select if you want the LLM's responses to show up in separate windows or integrated in the app's main window.
- Change the app's font size (affects every app element).
- Select which recipes.md file to use (useful if you want to keep different recipes files for different tasks).
- Enable Permanent Memory (buggy in the current version, saves all LLM responses in a subfolder within CoDude's folder).
- Logging Level (used for debugging - set it to minimal if everything works for you)
- Save any changes to the settings, or Cancel.

Remember that minimizing the app sends it back to the tray, but clicking Close will exit it and you'll have to run it again.

You'll have to keep CoDude running if you want its hotkey to work.

Pressing the hotkey again when its interface is visible will hide it again (I personally find it useful having it always available as a toggle).

And now, enjoy your seamless interractions with your LLMs with a single keypress!

## Installation & Use
1. Download the **recipes.md** you can find above, as well as the latest **CoDude.exe** from the **Releases** page (on the right). Save them in the same folder.
2. Install [LM Studio](https://lmstudio.ai/) (or any other solution that exposes an LLM through a single OpenAI-compatible URL).
3. Run LM Studio, visit its **Developer** page (green terminal icon) and **Select a model to load**. If you haven't downloaded any models, first pay a visit to the **Discover** page. Models that work nicely for text manipulation are... Well, anything over 3B like Mistral, Llama, Gemma, DeepSeek R1, Phi, and Qwen have worked for me.
4. Copy the URL from "**Reachable at**" you'll see at the top right of LM Studio's developer page.
5. Run CoDude, and press its default hotkey (**CTRL** + **Alt** + **C**) to see its window.
6. Click on the **CoDude** menu, and paste the URL you copied in the **LLM URL** field.
7. **Save** the changes and minimize CoDude. Done!
8. Select a string of text in **any** app. Copy it to the clipboard and hit CoDude's shortcut (by default **CTRL** + **Alt** + **C**).
9. Choose the recipe you want to "unleash" on the text.
10. Enjoy!

## LM Studio Tool Use
CoDude's recipes can now use LM Studio's MCP-based tools - for example, to perform web searches. However, enabling the feature requires some extra steps:

1. In LM Studio, move to its **Developer tab** (**CTRL** + **F2**).
2. Click **Server Settings**.
3. Enable **Require Authentication** and **Allow calling servers from mcp.json**.
4. Click **Manage Tokens**.
5. Click **Create new token**.
6. Add a name for CoDude, and set **Allow per-request remote MCP servers** and **Allow calling servers from mcp.json** to **Allow**. Then, click **Create token**.
7. **Copy** the created token.
8. Return to CoDude, click its main **CoDude** menu, and choose **Settings**.
9. Change the **LLM Provider** to **LM Studio Native API** (the other options don't support tool use. At least, not currently).
10. Paste the token you copied from LM Studio into **LM Studio API Token**.
11. Having tools enabled for every single command can induce delays, since even if a recipe doesn't **really** require the use of tools, the LLM might decide otherwise. To prevent the use of tools for every single recipe, and only enable it for specific recipes, place a checkmark on **Require USETOOLS keyword for tools**. Then, modify the prompt of the recipes where you want to enable tool use, to have them begin with "USETOOLS: ". Check the new "Suggest Links" recipe in recipes.md as an example.