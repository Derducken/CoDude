CoDude v0.0001

CoDude uses:
<a href="https://www.flaticon.com/free-icons/generator" title="generator icons">Generator icons created by edt.im - Flaticon</a>

Vibe-coded by multiple LLMs based on OK's instructions (plus some manual tweaking).

Ever heard of Microsoft's CoPilot? Awesome! This ain't it. CoDude's goal is to run as a resident app, lingering on your system's tray, waiting for its textual prey. When you use a key combination (by default, CTRL + Shift + `) to command CoDude to attack his target, he'll launch from the shadows, grab it, and offer you various options on how to deal with it. Will you have CoDude...

- Explain your text selection as if you're 2 years old?
- Summarize the selected text in three words, or less?
- Roast it as a disappointed superhero?

It's up to you. The app comes with at least ten preconfigured recipes that you'll see as buttons on its left side. I personally use those every day when "dealing with text" (reading it, writing it, painting over it with crayons while wheeping for humanity's non-future). If you don't like them, you can edit them, or replace them with your own. If you do like them you can still add more of your own. Generally, it would probably be best to add some of your own. The more, the better.

Or you could use the text area right under the prefoncigured recipes to type what you'd like CoDude to do with all that text you've selected. The whole three letters.

On the right of all that wonderful stuff you'll be able to see the text you selected in another editable textarea. You can manipulate it there before unleashing a command on it. Cut it down, streamline it, and edit it like a pro, while wondering "why the heck am I doing what I should have the LLM do for me in the first place? Is there any meaning in all of this? Help!".

The results of CoDude's hard work (...as a glorified intermediary between you and the configured LLM...) will pop up on a new window as soon as your LLM decides to grace us with its reply. From this window you can export this reply to a markdown file locally or copy it to the clipboard. There are buttons for that. They're clickable. GUI craftmanship at its finest.

The app's Configure button, on the bottom right, will show you yet another window with the following torrent of options:

- LLM's URL (OpenAI-friendly, like the "http://127.0.0.1:7777" I'm using in LM Studio, or "something like that").
- Recipes.md (Select a file with existing recipes or create a new one. Each recipe on its own single line, with an empty line between them. Each recipe should begin with the name you want to see in the respective app's button, bold, and followed by ":" and a space. For example, "Summarize: Please summarize the selected text". You can also group recipes using markdown syntax for headings. Each heading is parsed as a group, and all recipes under it as members of that group).
- 

Remember that minimizing the app sends it back to the tray, but clicking Close will exit it and you'll have to run it again. You'll have to keep it running if you want its hotkey to work. Pressing the hotkey again when its interface is visible will hide it again (I personally find it useful having it always available as a toggle).
