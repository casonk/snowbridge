# iPhone Shortcut

This document describes the closest native iPhone automation path for
`snowbridge`.

Apple Shortcuts does not expose a documented action that directly mounts an SMB
server in the background, so the practical native workflow is:

1. open the Files app automatically
2. keep the SMB address ready
3. reconnect from Recent Servers with one tap when needed

This repo does not include an importable `.shortcut` file because Apple creates
shareable shortcut files from the Shortcuts app itself. Use the build recipe
below, then export or share the shortcut from an iPhone, iPad, or Mac.

## Shortcut: Open Snowbridge

Create a shortcut named `Open Snowbridge` with these actions in order:

1. `Text`
   Set the text to your SMB server address, for example:

   ```text
   smb://snowbridge.local
   ```

   or:

   ```text
   smb://192.168.1.50
   ```

2. `Copy to Clipboard`
   Copy the text from step 1.

3. `Open App`
   Choose `Files`.

4. `Show Notification`
   Use a message such as:

   ```text
   Snowbridge address copied. In Files, tap Recent Servers or Connect to Server.
   ```

What this does:

- opens Files for you
- keeps the SMB address ready to paste
- reduces reconnect friction even though Shortcuts cannot fully mount the SMB
  share by itself

## Optional Personal Automation

If you want Files to open automatically when you join your home network, create
this personal automation:

1. In Shortcuts, go to `Automation`.
2. Tap `+`.
3. Tap `Create Personal Automation`.
4. Choose `Wi-Fi`.
5. Select your home SSID.
6. Add the action `Run Shortcut`.
7. Choose `Open Snowbridge`.
8. If your iPhone version offers it, turn off `Run After Confirmation` or the
   equivalent prompt so it runs automatically.

This still will not fully mount the SMB share in the background, but it will
open Files and prepare the address as soon as you join your home Wi-Fi.

## Import and Export

Once you build the shortcut on an Apple device, you can share it in the
Apple-supported ways below.

### Export as an iCloud Link

1. Open `Open Snowbridge` in the Shortcuts app.
2. Tap the share menu.
3. Choose `Copy iCloud Link` or share it through Messages, Mail, or another
   app.
4. On the receiving device, tap the link.
5. Tap `Get Shortcut`.

### Export as a File

1. Open `Open Snowbridge` in the Shortcuts app.
2. Open the share menu.
3. Choose `Export File` or `Options` then `File`, depending on platform.
4. Choose `Anyone` unless you specifically want the more restricted contact-only
   option.
5. Save the exported file to Files or send it through Messages or Mail.
6. On the receiving device, open the file and tap `Add Shortcut`.

## Import Questions

If you want to share the shortcut without hardcoding your host value, add an
import question to the SMB address field before exporting it.

Recommended import question:

- Question text: `What is your Snowbridge SMB address?`
- Default answer: `smb://snowbridge.local`

That lets each importing device set its own server address during `Get Shortcut`
or `Add Shortcut`.
