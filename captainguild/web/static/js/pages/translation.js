// Translation and conversions: the pack's switch, the answers to messages, flag translations and the DeepL Pro token.
import { html } from '../lib.js';
import { ANY_CHANNEL, Card, ChannelPicker, Col, Grid, NumberInput, SaveBar, SaveButton, SettingsGate, Switch, TextInput, useSettings } from '../ui.js';

export default function TranslationPage() {
  const form = useSettings('translation');
  return html`<${SettingsGate} form=${form}>${() => html`
    <${Grid}>
      <${Col}>
        <${Card} icon="repeat" title="Translate & convert" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Translation and conversions" help="Adds /translate, /currency, /units, /translate_language, /translate_autodelete, /export_emojis and the Translate menu to this server, and starts the answers below. Discord can take a minute to show new commands." ...${form.bind('enabled')} />
          <p class="muted small">Translations come from DeepL. Exchange rates come from a free daily rate list and are fetched at most once an hour.</p>
        <//>
        <${Card} icon="message-square" title="Answers to messages" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Translate messages that start with translate" help="Such as translate hola to english, or translate alone as a reply to a message. With no language named, the translation goes to the member's own language." ...${form.bind('answer_translate')} />
          <${Switch} label="Convert currencies in messages" help="Such as 20 bucks in euros or 100 usd eur." ...${form.bind('answer_currency')} />
          <${Switch} label="Convert units in messages" help="Such as 5 km to miles or 350F in C." ...${form.bind('answer_units')} />
          <${ChannelPicker} label="Channels left out" multi categories types=${ANY_CHANNEL} help="No answers to messages and no flag translations here. A category covers its channels." ...${form.bind('ignored_channels')} />
        <//>
      <//>
      <${Col}>
        <${Card} icon="flag" title="Flag translations" footer=${html`<${SaveButton} form=${form} />`}>
          <${Switch} label="Translate when someone reacts with a country's flag" help="The translation replies to the message in that country's language. One goes up for each message and language at a time." ...${form.bind('flag_translations')} />
          <${NumberInput} label="Delete them after" addon="seconds" min=${0} max=${3600} help="0 keeps them. The most is 3600." ...${form.bind('flag_delete_seconds')} />
        <//>
        <${Card} icon="key" title="DeepL Pro" footer=${html`<${SaveButton} form=${form} />`}>
          <${TextInput} label="DeepL Pro token" type="password" placeholder="Paste the token" help="Leave it empty for DeepL's free service. With a token, translations for this server go through that DeepL Pro account. It's kept for this server only and never shown again." ...${form.bind('deepl_token')} />
        <//>
      <//>
    <//>
    <${SaveBar} form=${form} />`}<//>`;
}
