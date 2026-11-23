// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Gerardo Bodegas Martinez
/*
  Five enhancements, and every page works with this file absent.

  The first is for the forms whose press takes a while, such as making a plan
  or uploading screenshots: say what the press is doing, keep saying so as the
  minutes pass, and let one press be one request from that form. The messages
  carry no closing punctuation; the pulsing dots after them are it. Only forms
  marked data-pending take part; a button marked so has its own words, said in
  place of the form's when it is the one pressed. A disabled button sends
  neither its name nor its value, so a named button's pair goes into a hidden
  field before the form's buttons are disabled, and that field goes when the
  page is shown again. Where the browser doesn't say which button was pressed,
  the form's words show and no button is disabled. Forms whose buttons carry a
  decision or a step are left alone. The later messages come from timers in
  this page, not from the server, so they say only that the wait goes on.

  The second opens a folded disclosure when a link on the page points inside
  it, so "What is shared" shows what it names.

  The third is for the review pages: when everything read is saved already,
  the page offers its Save button as "Save changes", so a type changed on a
  card is saved without this file. Here it shows as done until a type
  changed on a card or a question answered hands it back; a type the browser
  restored counts as changed. The card's own line says what the changed type
  does. On the grade review, an entry picked by hand from a row's list also
  selects that row's "Choose an existing assignment"; the server still checks
  the row's answers either way.

  The fourth is for a page shown again: by the browser's back/forward cache,
  from a copy it kept, or fetched again by Back, Forward or a refresh. A new
  Done the page met her with was seen when it first arrived, so it is retired
  where it stands: the petal holds still, its words are left out of what a
  reader hears, nothing moves and the focus stays. The petal opens on its
  first arrival only once this file has marked it. A page brought back from
  the cache keeps the focus where the reader left it. A page fetched again
  lands on the place its address names, as a link there does, instead of an
  offset kept from the page before her week regrouped; fetched again by Back
  or Forward with no such place, an answer that took the focus as it arrived
  gives it back, until the reader's first key or press, or two frames after
  the page loads.

  The fifth lists under a screenshot picker each screenshot chosen, by its full
  name, with its size and a preview this browser makes from the file, and says
  how many are chosen, since a phone hides the browser's own line with its
  button. Remove and Replace beside each change only what this browser holds
  as chosen, and send nothing. Once an upload goes ahead and the browser has
  taken the files it sends, the list and its pickers are held until the page
  is shown again, so the page never says it removed or replaced a file once
  the files are sent. Without this file the browser's line still shows, beside
  the page's line that choosing sends nothing.
*/
(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-pending]");
  var originalTitle = document.title;

  function say(form, words) {
    var note = form.querySelector(".pending");
    if (!note) {
      note = document.createElement("p");
      note.className = "pending";
      note.setAttribute("role", "status");
      note.setAttribute("aria-live", "polite");
      form.appendChild(note);
    }
    note.textContent = "";
    var text = document.createElement("span");
    text.textContent = words;
    var dots = document.createElement("span");
    dots.className = "dots";
    dots.setAttribute("aria-hidden", "true");
    for (var i = 0; i < 3; i += 1) {
      dots.appendChild(document.createElement("i"));
    }
    note.appendChild(text);
    note.appendChild(dots);
  }

  /* The submit buttons a form owns, wherever they stand on the page. */
  function pressesOf(form) {
    return Array.prototype.filter.call(form.elements, function (element) {
      return element.tagName === "BUTTON" && element.type === "submit";
    });
  }

  function reset(form) {
    delete form.dataset.busy;
    form.removeAttribute("aria-busy");
    (form.pendingTimers || []).forEach(clearTimeout);
    form.pendingTimers = [];
    pressesOf(form).forEach(function (button) {
      if (button.dataset.held) {
        delete button.dataset.held;
        button.disabled = false;
      }
      if (button.dataset.label) {
        button.textContent = button.dataset.label;
        delete button.dataset.label;
      }
    });
    var pressed = form.querySelector("input[data-pressed]");
    if (pressed) {
      pressed.remove();
    }
    var note = form.querySelector(".pending");
    if (note) {
      note.remove();
    }
    document.title = originalTitle;
  }

  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.busy) {
        event.preventDefault();
        return;
      }
      var pressed = event.submitter;
      var words = pressed && pressed.dataset.pending
        ? pressed.dataset.pending
        : form.dataset.pending;
      form.dataset.busy = "1";
      form.setAttribute("aria-busy", "true");
      if (pressed) {
        if (pressed.name) {
          var kept = document.createElement("input");
          kept.type = "hidden";
          kept.name = pressed.name;
          kept.value = pressed.value;
          kept.setAttribute("data-pressed", "");
          form.appendChild(kept);
        }
        pressesOf(form).forEach(function (button) {
          if (!button.disabled) {
            button.dataset.held = "1";
            button.disabled = true;
          }
        });
        pressed.dataset.label = pressed.textContent;
        pressed.textContent = words;
      }
      say(form, words);
      document.title = words;
      form.pendingTimers = [];
      if (form.dataset.pendingLater) {
        form.pendingTimers.push(setTimeout(function () {
          say(form, form.dataset.pendingLater);
        }, 20000));
      }
      if (form.dataset.pendingMuchLater) {
        form.pendingTimers.push(setTimeout(function () {
          say(form, form.dataset.pendingMuchLater);
        }, 60000));
      }
    });
  });

  function retire() {
    Array.prototype.forEach.call(document.querySelectorAll(".well-done"), function (met) {
      met.classList.add("seen");
      met.setAttribute("aria-hidden", "true");
    });
  }

  var arrival = window.performance && performance.getEntriesByType
    ? performance.getEntriesByType("navigation")[0]
    : null;
  var shownAgain = Boolean(arrival) && (arrival.type === "back_forward" || arrival.type === "reload");
  /* The element the address names, looked up as a browser does: the fragment as written,
     then with its escapes undone, where a percent sign that starts no escape is kept. */
  function placeNamed() {
    var fragment = location.hash.slice(1);
    if (!fragment) {
      return null;
    }
    var named = document.getElementById(fragment);
    if (named) {
      return named;
    }
    var kept = fragment.replace(/%(?![0-9A-Fa-f]{2})/g, "%25");
    try {
      return document.getElementById(decodeURIComponent(kept));
    } catch (error) {
      return null;
    }
  }

  var named = placeNamed();
  var landing = named && named.hasAttribute("tabindex") ? named : null;

  function land() {
    var fold = landing.closest("details");
    if (fold) {
      fold.open = true;
    }
    landing.scrollIntoView({block: "start"});
    landing.focus({preventScroll: true});
  }

  if (landing && "scrollRestoration" in history) {
    /* A return to this page lands on its place, not on an offset kept from before. */
    history.scrollRestoration = "manual";
  }
  /* A page shown again: fetched again by Back, Forward or a refresh, or brought back from
     the browser's back/forward cache as it was left. Either way a Done it met her with is
     retired. Brought back from the cache, the page and the focus are as the reader left them,
     and its controls are handed back. Fetched again, it lands on the place its address names,
     as a link there does; fetched again by Back or Forward with no such place, an answer
     that takes the focus as it arrives gives it back, so the page doesn't pull the focus
     from where the reader returned. */
  function giveBack() {
    function settle(event) {
      var focused = event && event.target && event.target.nodeType === 1
        ? event.target
        : document.activeElement;
      if (focused && focused !== document.body && focused.hasAttribute("autofocus")) {
        focused.blur();
      }
    }
    function done() {
      document.removeEventListener("focusin", settle, true);
      document.removeEventListener("keydown", done, true);
      document.removeEventListener("pointerdown", done, true);
    }
    settle();
    document.addEventListener("focusin", settle, true);
    document.addEventListener("keydown", done, true);
    document.addEventListener("pointerdown", done, true);
    /* The browser gives an answer the focus in the first frame after the page loads. */
    window.addEventListener("load", function () {
      requestAnimationFrame(function () {
        requestAnimationFrame(done);
      });
    });
  }

  function returned(how) {
    retire();
    if (how === "restored") {
      Array.prototype.forEach.call(forms, reset);
    } else if (landing) {
      land();
      window.addEventListener("load", land);
    } else if (how === "back_forward") {
      giveBack();
    }
  }

  if (shownAgain) {
    returned(arrival.type);
  }
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      returned("restored");
    }
  });

  /* The petal opens only now that a restoration can retire it; where this file
     can't run, the stylesheet keeps it still. */
  if (!shownAgain) {
    Array.prototype.forEach.call(document.querySelectorAll(".well-done"), function (met) {
      met.classList.add("fresh");
    });
  }

  var review = document.querySelector("form.review-form");
  if (review) {
    review.addEventListener("change", function (event) {
      var name = event.target.name || "";
      /* An entry picked by hand from a grade row's list also answers the row with "Choose an
         existing assignment". A value the browser restores sends no change, and one a script
         sets isn't trusted, so neither moves the answer. */
      if (event.isTrusted && name.indexOf("choose.") === 0 && event.target.value) {
        var choose = review.querySelector(
          "input[type='radio'][name='match." + name.slice(7) + "'][value='choose']"
        );
        if (choose) {
          choose.checked = true;
        }
      }
      if (name.indexOf("kind-") !== 0 && name.indexOf("occurrence-") !== 0) {
        return;
      }
      if (name.indexOf("kind-") === 0) {
        var card = event.target.closest("article");
        var effect = card ? card.querySelector(".effect") : null;
        if (effect) {
          var base = effect.dataset.base === undefined ? effect.textContent : effect.dataset.base;
          var chosen = event.target.value === "TASK" ? "task" : "homework";
          /* A choice carried from a page before, marked beside the select,
             is the parent's whatever it is set to, the suggestion included. */
          var carried = card.querySelector("input[name='chosen-" + name.slice(5) + "']");
          effect.textContent = event.target.value === event.target.dataset.saved && !carried
            ? base
            : (base ? base + " " : "") + "The type becomes " + chosen + ", as chosen.";
        }
      }
      var button = review.querySelector("button.primary[disabled]");
      if (button) {
        button.disabled = false;
        button.removeAttribute("aria-disabled");
        button.classList.remove("done");
        button.textContent = button.dataset.changedLabel || button.textContent;
      }
    });
  }

  /* The page's own Save press shows as done while every Type reads as the page wrote it,
     and as "Save changes" once one differs, checked again when the page is shown, since the
     browser may restore a Type after this file first runs. */
  var unchanged = review ? review.querySelector("button.primary[data-unchanged-label]") : null;

  function reconcile() {
    var restored = Array.prototype.some.call(
      review.querySelectorAll("select[name^='kind-']"),
      function (select) {
        var shown = select.querySelector("option[selected]");
        return shown !== null && select.value !== shown.value;
      }
    );
    unchanged.disabled = !restored;
    unchanged.classList.toggle("done", !restored);
    if (restored) {
      unchanged.removeAttribute("aria-disabled");
      unchanged.textContent = unchanged.dataset.changedLabel;
    } else {
      unchanged.setAttribute("aria-disabled", "true");
      unchanged.textContent = unchanged.dataset.unchangedLabel;
    }
  }

  if (unchanged) {
    reconcile();
    window.addEventListener("pageshow", reconcile);
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href^='#']") : null;
    if (!link) {
      return;
    }
    var target = document.querySelector(link.getAttribute("href"));
    var fold = target ? target.closest("details") : null;
    if (fold) {
      fold.open = true;
    }
  });

  /* What a screenshot picker holds, listed beneath it: each file's full name, its size, where a
     megabyte is a million bytes as in the upload limits, and a preview this browser makes from
     the file itself. Remove and Replace change only what the picker holds. Replace opens a
     picker for one file that carries no name, so no form sends it. Where the browser can't
     change what a picker holds, the names and previews show and choosing again changes them. */
  var editable = (function () {
    try {
      return typeof DataTransfer === "function" && Boolean(new DataTransfer().items);
    } catch (error) {
      return false;
    }
  })();
  var previewing = Boolean(window.URL && URL.createObjectURL && URL.revokeObjectURL);

  function sized(bytes) {
    return bytes >= 1000000
      ? (bytes / 1000000).toFixed(1) + " MB"
      : Math.max(1, Math.round(bytes / 1000)) + " KB";
  }

  Array.prototype.forEach.call(document.querySelectorAll("input[type='file']"), function (picker) {
    var form = picker.form;
    var line = form ? form.querySelector("[data-chosen]") : null;
    if (!line) {
      return;
    }
    var list = form.querySelector("[data-chosen-files]");
    var previews = [];
    var swap = null;
    var swapping = -1;
    var locked = false;

    function release() {
      previews.forEach(function (url) {
        URL.revokeObjectURL(url);
      });
      previews = [];
    }

    function chosen() {
      return picker.files ? Array.prototype.slice.call(picker.files) : [];
    }

    function hold(files) {
      var transfer = new DataTransfer();
      files.forEach(function (file) {
        transfer.items.add(file);
      });
      picker.files = transfer.files;
    }

    function press(words, file, act) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = words;
      var whose = document.createElement("span");
      whose.className = "visually-hidden";
      whose.textContent = " " + file.name;
      button.appendChild(whose);
      button.addEventListener("click", act);
      return button;
    }

    function row(file, place) {
      var item = document.createElement("li");
      if (previewing) {
        var frame = document.createElement("span");
        var preview = document.createElement("img");
        var url = URL.createObjectURL(file);
        previews.push(url);
        preview.alt = "";
        preview.src = url;
        frame.appendChild(preview);
        item.appendChild(frame);
      }
      var about = document.createElement("div");
      var name = document.createElement("p");
      name.textContent = file.name;
      var size = document.createElement("p");
      size.className = "note";
      size.textContent = sized(file.size);
      about.appendChild(name);
      about.appendChild(size);
      if (editable) {
        var actions = document.createElement("div");
        actions.className = "actions";
        actions.appendChild(press("Remove", file, function () {
          remove(place);
        }));
        actions.appendChild(press("Replace", file, function () {
          replace(place);
        }));
        about.appendChild(actions);
      }
      item.appendChild(about);
      return item;
    }

    /* The status line says what changed, then how many are chosen. */
    function show(said) {
      release();
      var files = chosen();
      var words = said ? [said] : [];
      if (files.length) {
        var one = files.length === 1;
        words.push(files.length + (one ? " screenshot chosen." : " screenshots chosen."));
      } else if (said) {
        words.push("No screenshots chosen.");
      }
      if (files.length && list && !editable) {
        words.push("To change what you chose, choose again.");
      }
      line.textContent = words.join(" ");
      if (!list) {
        return;
      }
      list.textContent = "";
      files.forEach(function (file, place) {
        list.appendChild(row(file, place));
      });
      list.hidden = files.length === 0;
    }

    /* While an upload is under way, the picker, the Replace picker and each file's Remove and
       Replace are held, and no step below changes what is chosen or what the line says of it. */
    function lock(holding) {
      locked = holding;
      picker.disabled = holding;
      if (swap) {
        swap.disabled = holding;
      }
      Array.prototype.forEach.call(list ? list.querySelectorAll("button") : [], function (button) {
        button.disabled = holding;
      });
    }

    /* After a Remove the focus goes to the next file's Remove, or the one before it when the
       last file went, or to the picker when none is left. */
    function remove(place) {
      if (locked) {
        return;
      }
      var files = chosen();
      var gone = files.splice(place, 1)[0];
      hold(files);
      show("Removed " + gone.name + ".");
      var left = list.querySelectorAll(".actions button:first-child");
      (left[place] || left[place - 1] || picker).focus();
    }

    function replace(place) {
      if (locked) {
        return;
      }
      if (!swap) {
        swap = document.createElement("input");
        swap.type = "file";
        swap.accept = picker.accept;
        swap.hidden = true;
        swap.addEventListener("change", swapped);
        list.parentNode.insertBefore(swap, list.nextSibling);
      }
      swapping = place;
      swap.value = "";
      swap.click();
    }

    /* The file chosen takes the place of the one replaced, and the focus stays on its Replace. */
    function swapped() {
      if (locked) {
        return;
      }
      var files = chosen();
      var into = swap.files ? swap.files[0] : null;
      if (!into || swapping < 0 || swapping >= files.length) {
        return;
      }
      var out = files[swapping];
      files[swapping] = into;
      hold(files);
      show("Replaced " + out.name + " with " + into.name + ".");
      var again = list.querySelectorAll(".actions button:last-child")[swapping];
      if (again) {
        again.focus();
      }
    }

    function chose() {
      if (locked) {
        return;
      }
      show("");
    }

    function handBack() {
      lock(false);
      show("");
    }

    /* A browser fires formdata as it builds what a submission sends, once the submit went
       ahead, with the picker's files already in it. A disabled field sends nothing, so the
       list is held only from here, and only on a form the submit marked busy; every showing of
       the page hands it back. */
    form.addEventListener("formdata", function () {
      if (form.dataset.busy) {
        lock(true);
      }
    });
    picker.addEventListener("change", chose);
    window.addEventListener("pageshow", handBack);
    window.addEventListener("pagehide", release);
  });
})();
