/**
 * Builds the CastleRAG user-study Google Form (refined version of
 * "CastleRAG User Study — Questionnaire").
 *
 * Run buildForm() once. It creates the form in your Drive and logs the
 * edit and respondent links.
 *
 * Screenshots and footage links: after the live capture
 * (scripts/study_screenshots.py), run scripts/study_form_export.py with the
 * chosen slots. It writes orientation.png and <slot>_01_answer.png ...
 * <slot>_04_refined.png, plus form_constants.js with TASKS and FOOTAGE.
 * Upload the PNGs to one Drive folder, set SCREENSHOT_FOLDER_ID, paste the
 * constants over the ones below, and rebuild. Missing files become a visible
 * "[screenshot: name]" placeholder, so the form can be piloted before the
 * capture run.
 */

const SCREENSHOT_FOLDER_ID = '';  // Drive folder id with the screenshots, or '' for placeholders
const FORM_TITLE = 'CastleRAG user study';

// Tentative task picks from the 2026-10-12 benchmark runs on the full index.
// Confirm against the live capture (manifest.json) before sending the form.
// role is for the analysis only and never shown to participants.
const TASKS = {
  practice: { question: "What organisation's logo is on Werner's apron?" },
  t1: { question: 'What game did Bjorn suggest to play with the cameras?', role: 'right, well supported' },
  t2: { question: 'Who brought the coffee pot to the table for breakfast on the first day?', role: 'right, well supported' },
  t3: { question: 'Who won the first round of UNO?', role: 'right, weakly supported' },
  t4: { question: 'Who gave the first presentation for the workshop on the first day?', role: 'wrong' },
};

// Seeked YouTube links per slot (camera -> watch_url), from form_constants.js.
const FOOTAGE = { practice: {}, t1: {}, t2: {}, t3: {}, t4: {} };

const ORDER_A = ['t1', 't2', 't3', 't4'];
const ORDER_B = ['t4', 't3', 't2', 't1'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

function buildForm() {
  const form = FormApp.create(FORM_TITLE);
  form.setDescription(
    'CastleRAG is a research prototype that answers questions about a day of ' +
    'recorded video from wearable and room cameras, and shows the footage it ' +
    'used as evidence. You will see screenshots of it and judge its answers. ' +
    'This takes about 15 to 20 minutes.');
  form.setCollectEmail(false);
  form.setLimitOneResponsePerUser(false);
  form.setProgressBar(true);
  form.setShuffleQuestions(false);
  form.setAllowResponseEdits(false);
  form.setConfirmationMessage(
    'Thank you for taking part. One thing we could not say up front: some of ' +
    'the answers you judged were wrong on purpose, or only weakly backed by the ' +
    'footage, because we want to know whether the evidence helps people spot ' +
    'that. Please do not tell other participants which ones.');

  const images = loadImages_();

  // Section 1: consent (first page, no page break before it).
  form.addSectionHeaderItem().setTitle('Before you start').setHelpText(
    'Your responses are anonymous: we do not collect names or email addresses. ' +
    'They will be analysed by the CastleRAG team at the University of Amsterdam ' +
    'and reported in aggregate in a research publication. You can stop at any ' +
    'time by closing this page; nothing is saved until you submit.');
  form.addCheckboxItem()
    .setTitle('Consent')
    .setChoiceValues(['I agree to take part, and to my anonymous responses being used in this research and its publication.'])
    .setRequired(true);

  // Section 3: orientation and comprehension checks.
  form.addPageBreakItem().setTitle('How CastleRAG works');
  addImage_(form, images, 'orientation.png',
    '(1) Your question and the system\'s answer, with a badge saying how well the footage backs it: ' +
    'Well supported (green), Partial support (yellow) or Low confidence (red). ' +
    '(2) The moment the answer relies on, played from up to three cameras that recorded it at the same time. ' +
    '(3) For each camera you can choose Confirm, Refine or Ignore. ' +
    '(4) The system turns your choices into a sharper search and runs it again.');
  form.addMultipleChoiceItem().setTitle('What does the yellow "Partial support" badge mean?')
    .setChoiceValues(['The answer is wrong', 'The footage only partly backs the answer',
      'The video has only partly loaded', "I don't know"]).setRequired(true);
  form.addMultipleChoiceItem().setTitle('The three videos side by side show...')
    .setChoiceValues(['Three different moments', 'The same moment, recorded by three cameras',
      'Three possible answers', "I don't know"]).setRequired(true);

  // Practice task (shared by both versions).
  addTask_(form, images, 'practice', 'Practice task',
    'This one is for practice and is not analysed.');

  // About you. Placed after the practice task: the birth-month answer
  // routes to version A or B when this page is left.
  form.addPageBreakItem().setTitle('About you');
  form.addMultipleChoiceItem().setTitle('Which best describes you?')
    .setChoiceValues(['BSc student', 'MSc student', 'PhD student or researcher', 'Working in industry'])
    .showOtherOption(true).setRequired(true);
  form.addMultipleChoiceItem().setTitle('How often do you use AI assistants such as ChatGPT or Claude?')
    .setChoiceValues(['Never', 'A few times a month', 'Weekly', 'Daily']).setRequired(true);
  form.addScaleItem().setTitle('How familiar are you with machine learning or search systems?')
    .setBounds(1, 5).setLabels('Not at all', 'I build them').setRequired(true);
  form.addMultipleChoiceItem().setTitle('Have you seen the CASTLE 2024 dataset or its videos before?')
    .setChoiceValues(['Yes', 'No']).setRequired(true);
  const month = form.addListItem().setTitle('Your birth month')
    .setHelpText('Only used to decide the order in which you see the tasks.').setRequired(true);

  // Version A and B task chains. In Forms a jump is attached to the page
  // break that ends a section, so: chain A's last section ends at chain B's
  // first page break, which jumps to "overall"; chain B flows into it.
  const chainA = addChain_(form, images, ORDER_A, 'A');
  const chainB = addChain_(form, images, ORDER_B, 'B');
  const overall = form.addPageBreakItem().setTitle('Nearly done')
    .setHelpText('A few questions about CastleRAG overall.');
  chainB.firstPage.setGoToPage(overall);

  // Birth month routing: odd months to A, even to B.
  month.setChoices(MONTHS.map((m, i) =>
    month.createChoice(m, i % 2 === 0 ? chainA.firstPage : chainB.firstPage)));

  // Overall impression (UEQ-S): 7-point semantic differentials.
  form.addSectionHeaderItem().setTitle('Overall impression')
    .setHelpText('Rate CastleRAG between each pair of words.');
  [['obstructive', 'supportive'], ['complicated', 'easy'], ['inefficient', 'efficient'],
   ['confusing', 'clear'], ['boring', 'exciting'], ['not interesting', 'interesting'],
   ['conventional', 'inventive'], ['usual', 'leading edge']].forEach(([l, r], i) =>
    form.addScaleItem().setTitle(`UEQ ${i + 1}: ${l} / ${r}`)
      .setBounds(1, 7).setLabels(l, r).setRequired(true));

  // Trust and usefulness.
  form.addPageBreakItem().setTitle('Trust and usefulness')
    .setHelpText('1 = strongly disagree, 7 = strongly agree.');
  ['I am confident in CastleRAG.', 'CastleRAG is reliable.', 'I can trust CastleRAG.',
   'CastleRAG is dependable.', "I am suspicious of CastleRAG's answers.", 'I am wary of CastleRAG.',
   'Seeing the footage made it clear why CastleRAG gave its answer.',
   'A tool like this would help me answer questions about long recordings faster than scrubbing through the video myself.',
   'Seeing several cameras at the same moment helped me judge the evidence.']
    .forEach(t => agree_(form, t));

  // Open questions.
  form.addPageBreakItem().setTitle('Your comments');
  ['What was most useful?', 'What was confusing or missing?',
   "When would you not trust CastleRAG's answer?", 'Anything else?']
    .forEach(t => form.addParagraphTextItem().setTitle(t));

  Logger.log('Edit:    ' + form.getEditUrl());
  Logger.log('Respond: ' + form.getPublishedUrl());
  return form.getEditUrl();
}

function addChain_(form, images, order, version) {
  let firstPage = null, lastPage = null;
  order.forEach((key, idx) => {
    const pages = addTask_(form, images, key, `Task ${idx + 1} of 4`, '', `${version}-${key}`);
    if (!firstPage) firstPage = pages.first;
    lastPage = pages.last;
  });
  return { firstPage, lastPage };
}

/** One task over three pages. tag prefixes item titles so A and B can be merged in analysis. */
function addTask_(form, images, key, heading, note, tag) {
  const t = TASKS[key];
  const p = tag ? `[${tag}] ` : '[practice] ';
  const first = form.addPageBreakItem().setTitle(heading)
    .setHelpText((note ? note + '\n\n' : '') + 'Question asked: ' + t.question);
  addImage_(form, images, `${key}_01_answer.png`, 'CastleRAG\'s answer.');
  form.addMultipleChoiceItem().setTitle(p + "Do you think CastleRAG's answer is correct?")
    .setChoiceValues(['Yes', 'No', "Can't tell"]).setRequired(true);
  form.addScaleItem().setTitle(p + 'How confident are you?')
    .setBounds(1, 7).setLabels('Guessing', 'Certain').setRequired(true);

  form.addPageBreakItem().setTitle(heading + ': the evidence')
    .setHelpText('Here is the footage the answer relies on. ' + footageText_(key));
  addImage_(form, images, `${key}_02_evidence.png`, 'The evidence moment.');
  form.addMultipleChoiceItem().setTitle(p + 'Did you open any of the video links?')
    .setChoiceValues(['No', 'Yes, one', 'Yes, more than one']).setRequired(true);
  form.addMultipleChoiceItem().setTitle(p + 'Does this evidence back up the answer?')
    .setChoiceValues(['Yes', 'Partly', 'It contradicts it', "Can't tell"]).setRequired(true);
  form.addMultipleChoiceItem().setTitle(p + 'Now, do you think the answer is correct?')
    .setChoiceValues(['Yes', 'No', "Can't tell"]).setRequired(true);
  form.addScaleItem().setTitle(p + 'How confident are you now?')
    .setBounds(1, 7).setLabels('Guessing', 'Certain').setRequired(true);
  form.addMultipleChoiceItem().setTitle(p + 'What would you do next?')
    .setChoiceValues(['Accept the answer', 'Search again with a refined question', 'Reject the answer', 'Not sure'])
    .setRequired(true);
  form.addParagraphTextItem().setTitle(p + 'Why? (optional)');

  const last = form.addPageBreakItem().setTitle(heading + ': refining the search')
    .setHelpText('Someone reviewed each camera and the system drafted a new search from those choices. ' +
      'The second image shows the result.');
  addImage_(form, images, `${key}_03_review.png`, 'Per-camera choices and the drafted search.');
  addImage_(form, images, `${key}_04_refined.png`, 'The refined result.');
  agree_(form, p + 'The drafted search reflects what I would have wanted.');
  agree_(form, p + 'After the refinement, the answer is more convincing.');
  form.addScaleItem().setTitle(p + 'How mentally demanding was it to judge this answer?')
    .setBounds(1, 7).setLabels('Very low', 'Very high').setRequired(true);
  return { first, last };
}

function agree_(form, title) {
  form.addScaleItem().setTitle(title).setBounds(1, 7)
    .setLabels('Strongly disagree', 'Strongly agree').setRequired(true);
}

function footageText_(key) {
  const links = Object.entries(FOOTAGE[key] || {});
  if (!links.length) return '[footage links: fill FOOTAGE in the script from manifest.json]';
  return 'You can watch it yourself: ' + links.map(([cam, url]) => `${cam}: ${url}`).join('  ·  ');
}

function loadImages_() {
  const out = {};
  if (!SCREENSHOT_FOLDER_ID) return out;
  const it = DriveApp.getFolderById(SCREENSHOT_FOLDER_ID).getFiles();
  while (it.hasNext()) { const f = it.next(); out[f.getName()] = f.getBlob(); }
  return out;
}

function addImage_(form, images, name, caption) {
  if (images[name]) {
    form.addImageItem().setImage(images[name]).setTitle(caption);
  } else {
    form.addSectionHeaderItem().setTitle(`[screenshot: ${name}]`).setHelpText(caption);
  }
}

/**
 * Logs every page and where it routes, to check the A/B branching of a built
 * form. Pass the form id from its edit URL.
 */
function checkRouting(formId) {
  const form = FormApp.openById(formId);
  const out = [];
  form.getItems().forEach(it => {
    if (it.getType() === FormApp.ItemType.PAGE_BREAK) {
      const pb = it.asPageBreakItem(); const g = pb.getGoToPage();
      out.push(`PAGE ${it.getIndex()} "${pb.getTitle()}"` +
        (g ? ` | arriving linearly jumps to ${g.getIndex()} "${g.getTitle()}"` : ''));
    }
    if (it.getType() === FormApp.ItemType.LIST) {
      it.asListItem().getChoices().forEach(c => out.push(`  ${c.getValue()} -> ` +
        (c.getGotoPage() ? `${c.getGotoPage().getIndex()} ${c.getGotoPage().getTitle()}` : 'next')));
    }
  });
  out.push('items: ' + form.getItems().length);
  Logger.log(out.join('\n'));
}

/**
 * Fills a form built by buildForm() with the capture results, in place, so
 * its links stay the same: every "[screenshot: name]" placeholder whose file
 * is in SCREENSHOT_FOLDER_ID becomes an image, and each task's question and
 * footage links are rewritten from TASKS and FOOTAGE (slots are found from
 * the [practice]/[A-t1]/[B-t3] tags on the task items).
 */
function applyCapture(formId) {
  const form = FormApp.openById(formId);
  const images = loadImages_();
  let swapped = 0, missing = [];
  // Placeholders, last first so earlier indexes stay valid.
  form.getItems(FormApp.ItemType.SECTION_HEADER).reverse().forEach(it => {
    const m = /^\[screenshot: (.+)\]$/.exec(it.getTitle());
    if (!m) return;
    const blob = images[m[1]];
    if (!blob) { missing.push(m[1]); return; }
    const idx = it.getIndex();
    const img = form.addImageItem().setImage(blob).setTitle(it.asSectionHeaderItem().getHelpText());
    form.moveItem(img.getIndex(), idx);
    form.deleteItem(idx + 1);
    swapped++;
  });
  // Task pages: each page takes the slot of the first tagged item after it.
  const items = form.getItems();
  const pages = [];
  items.forEach(it => {
    if (it.getType() === FormApp.ItemType.PAGE_BREAK) { pages.push({ pb: it.asPageBreakItem(), slot: null }); return; }
    const m = /^\[(?:[AB]-)?(practice|t[1-4])\] /.exec(it.getTitle());
    const page = pages[pages.length - 1];
    if (m && page && !page.slot) page.slot = m[1];
  });
  let pagesSet = 0;
  pages.forEach(({ pb, slot }) => {
    if (!slot) return;
    const h = pb.getHelpText();
    if (h.includes('Question asked: ')) {
      pb.setHelpText(h.replace(/Question asked: .*$/s, 'Question asked: ' + TASKS[slot].question));
      pagesSet++;
    } else if (h.startsWith('Here is the footage the answer relies on.')) {
      pb.setHelpText('Here is the footage the answer relies on. ' + footageText_(slot));
      pagesSet++;
    }
  });
  Logger.log(`images placed: ${swapped}; still missing: ${missing.length ? missing.join(', ') : 'none'}; task pages updated: ${pagesSet}`);
}
