#ifndef USB_WYOMING_H
#define USB_WYOMING_H
/* Existing USB driver must be installed and exclusively owned by caller.
 * Sends mode acknowledgment only after resource/identity preparation.
 * Returning is terminal: caller must latch until reset, never resume commands.
 * No audio/network initialization. Factory MAC is read, never written.
 */
int usb_wyoming_run(void);
#endif
