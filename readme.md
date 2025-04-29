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

You can group commands by using typical Markdown headings. Every heading is automatically recognized as a group, and the commands underneath it as members of that group. Checking the recipes.md file in combination of "how the default commands show up on the app's interface" will help you make better sense of "how it works".

Each recipe and heading must be followed by an empty line in the markdown file.

Apart from predefined commands, you can also use the Custom Input text field to type a custom command describing "what you want to do to the text you've copied to the Clipboard". You can send that command to the LLM (together with the text you've selected) with a click on Send Custom Command, or by pressing CTRL+Enter.


On the right of all that wonderful stuff you'll be able to see the text you selected in another editable textarea. You can manipulate it there before unleashing a command on it. Cut it down, streamline it, and edit it like a pro, while wondering "why the heck am I doing what I should have the LLM do for me in the first place? Is there any meaning in all of this? Help!".

The results of CoDude's hard work (...as a glorified intermediary between you and the configured LLM...) will pop up on a new window as soon as your LLM decides to grace us with its reply. From this window you can export this reply to a markdown file locally or copy it to the clipboard. There are buttons for that. They're clickable. GUI craftmanship at its finest.

However, if you don't like all those pop up windows (that are also somewhat buggy in the current version), you can choose to have the LLM responses integrated as a third column on CoDude's main window, from the app's Configuration.

Which brings us to...

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