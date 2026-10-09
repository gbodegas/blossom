// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Gerardo Bodegas Martinez
/*
  Four enhancements, and every page works with this file absent.

  The first is for the forms that make a plan: say that a plan is being made,
  keep saying so as the minutes pass, and let one press be one request from
  that form. The messages carry no closing punctuation; the pulsing dots after
  them are it. Only forms marked data-pending take part, and their submit
  buttons carry no name, so disabling one changes nothing about what is sent.
  Forms whose buttons carry a decision or a step are left alone. The later
  messages come from timers in this page, not from the server, so they say
  only that the wait goes on.

  The second opens a folded disclosure when a link on the page points inside
  it, so "What is shared" shows what it names.

  The third is for the review pages: when everything read is saved already,
  its Save button is disabled, and a type changed on a card or a question
  answered hands the button back, as "Save changes"; the card's own line
  says what the changed type does. On the grade review, an entry picked by
  hand from a row's list also selects that row's "Choose an existing
  assignment"; the server still checks the row's answers either way.

  The fourth is for a page shown again: by the browser's back/forward cache,
  from a copy it kept, or fetched again by Back, Forward or a refresh. A new
  Done the page met her with was seen when it first arrived, so it is retired
  where it stands: the petal holds still, its words are left out of what a
  reader hears, nothing moves and the focus stays. The petal opens on its
  first arrival only once this file has marked it. A page fetched again lands
  on the place its address names, as a link there does, instead of an offset
  kept from the page before her week regrouped.
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

  function reset(form) {
    delete form.dataset.busy;
    form.removeAttribute("aria-busy");
    (form.pendingTimers || []).forEach(clearTimeout);
    form.pendingTimers = [];
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = false;
      if (button.dataset.label) {
        button.textContent = button.dataset.label;
      }
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
      form.dataset.busy = "1";
      form.setAttribute("aria-busy", "true");
      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.dataset.label = button.textContent;
        button.disabled = true;
        button.textContent = form.dataset.pending;
      }
      say(form, form.dataset.pending);
      document.title = form.dataset.pending;
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
  if (shownAgain) {
    retire();
    if (landing) {
      land();
      window.addEventListener("load", land);
    }
  }

  /* A page restored from the browser's cache comes back as it was left, busy
     included; the controls are handed back, and a Done it met her with is retired. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      Array.prototype.forEach.call(forms, reset);
      retire();
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
})();
